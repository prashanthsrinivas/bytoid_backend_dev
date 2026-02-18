from flask import Blueprint, request, jsonify, session
import pymysql
import os
import uuid
from db.rds_db import connect_to_rds
import json
from datetime import datetime
from utils.base_logger import get_logger
from werkzeug.utils import secure_filename
from utils.s3_utils import attach_CLDFRNT_url, generate_presigned_url, upload_any_file
from werkzeug.security import generate_password_hash, check_password_hash
import re
from dotenv import load_dotenv
from invited_users.uszr_helper import generate_hashed_url
from services.gmail_service import GmailService
from db.db_checkers import check_onboarding_user
from cryptography.fernet import Fernet
import base64
import time

users_bp = Blueprint("users", __name__)

logger = get_logger(__name__)

SECRET_KEY = os.getenv("SECRETKEY")
fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY.encode("utf-8").ljust(32)[:32]))
# def get_db_connection():
#     connection = pymysql.connect(
#         host='database-1.czoeckiiosd2.ap-south-1.rds.amazonaws.com',
#         user='skilbyt_db',
#         password='JesusChristIsLord$1',
#         database='ai_support'
#     )
#     return connection
def format_address(door, unit, street, zip_code):
    parts = [
        f"Door {door}" if door else None,
        f"Unit {unit}" if unit else None,
        street,
        f"ZIP {zip_code}" if zip_code else None,
    ]
    return ", ".join([p for p in parts if p])


@users_bp.route("/onboarding", methods=["POST"])
def submit_onboarding():
    try:
        payload = request.get_json()
        # print("onboarding", payload)

        # user_id = session.get("user_id") or payload.get("user_id")
        user_id = payload.get("user_id")

        data = payload.get("data", {})

        conn = connect_to_rds()
        cursor = conn.cursor()

        ProofOfBusinessFile = request.files.get("businessProof_filename", "")
        BusinessImageFile = request.files.get("businessImage_filename", "")
        # print("proffbusiness", ProofOfBusinessFile)
        # print("businessimage", BusinessImageFile)
        # Prepare sociallinks JSON
        sociallinks = {
            "whatsapp": data.get("whatsappNumber"),
            "facebook": data.get("facebookId"),
            "instagram": data.get("instagramId"),
            "linkedin": data.get("linkedinId"),
            "slack": data.get("slackId"),
            "teams": data.get("teamsId"),
            "shopify": data.get("shopifyId"),
            "woocommerce": data.get("woocommerceUrl"),
        }

        # Update users table
        cursor.execute(
            """
            UPDATE users
            SET first_name = %s,
                phone = %s,
                sociallinks = %s,
                updated_in = %s
            WHERE user_id = %s
        """,
            (
                data.get("name"),
                data.get("primaryPhone"),
                json.dumps(sociallinks),
                datetime.utcnow(),
                user_id,
            ),
        )

        # Generate business_info_id
        business_info_id = str(uuid.uuid4())

        # Prepare address strings
        billing_address = f"{data.get('doorNumber', '')}, {data.get('unitNumber', '')}, {data.get('streetName', '')}, {data.get('zipCode', '')}"
        shipping_address = f"{data.get('shippingDoorNumber', '')}, {data.get('shippingUnitNumber', '')}, {data.get('shippingStreetName', '')}, {data.get('shippingZipCode', '')}"

        # Normalize enums
        cognitive = data.get("cognitype", "").capitalize()
        reg_status = (
            "Registered"
            if data.get("registrationStatus") == "registered"
            else "Non-Registered"
        )

        # Insert into business_info
        cursor.execute(
            """
            INSERT INTO business_info (
                business_info_id, user_id_fk, BusinessName, Age, Sex, LineOfBusiness,
                YearsInBusiness, HasLicense, RegistrationStatus, ProofOfBusinessFile, RegistrationNumber,
                GSTNumber, Country, ProvinceOrState, City, BillingAddress, ShippingAddress, BusinessImage,
                BusinessEmail, PaymentMethods, PaymentDetails, OwnershipType, BusinessTimings,
                WebsiteUrl, SecondaryPhone, GSTNotAvailable, SameAsBilling,businessLocation
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,%s)
        """,
            (
                business_info_id,
                user_id,
                data.get("businessName"),
                str(data.get("age")),
                cognitive,
                data.get("lineOfBusiness"),
                str(data.get("yearsInBusiness")),
                data.get("hasLicense", False),
                reg_status,
                str(data.get("businessProof", {})),
                data.get("registrationNumber", ""),
                data.get("gstNumber", ""),
                data.get("country"),
                data.get("province"),
                data.get("city"),
                billing_address,
                shipping_address,
                str(data.get("businessImage", {})),
                data.get("businessEmail", ""),
                json.dumps(data.get("paymentMethods", [])),
                json.dumps(data.get("paymentDetails", {})),
                data.get("ownershipType"),
                data.get("businessTimings"),
                data.get("websiteUrl"),
                data.get("secondaryPhone"),
                data.get("gstNotAvailable", False),
                data.get("sameAsBilling", False),
                data.get("businessLocation", ""),
            ),
        )

        conn.commit()

        # print(" business_info_id , user_id : ")
        # print(f" {business_info_id} | {user_id}")
        cursor.execute(
            """
                SELECT business_info_id FROM business_info WHERE user_id_fk = %s
            """,
            (str(user_id),),
        )
        row = cursor.fetchone()

        # if not row:
        #         #print(f"not found business_info_id")
        # else:
        #     #print(f"business_info_id found : {row[0]}")

        cursor.close()
        conn.close()

        return (
            jsonify(
                {"status": "success", "message": "Onboarding data saved successfully."}
            ),
            200,
        )

    except Exception as e:
        # print("Error in onboarding:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@users_bp.route("/get_onboarding", methods=["GET"])
def get_onboarding():
    try:
        user_id = request.args.get("user_id")
        if not user_id:
            return jsonify({"status": "error", "message": "user_id is required"}), 400

        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        # Fetch user info
        cursor.execute(
            "SELECT user_id, first_name, email, phone, sociallinks FROM users WHERE user_id = %s",
            (user_id,),
        )
        user = cursor.fetchone()

        if not user:
            return jsonify({"status": "error", "message": "User not found"}), 404

        # Parse sociallinks
        if user.get("sociallinks"):
            try:
                user["sociallinks"] = json.loads(user["sociallinks"])
            except Exception:
                user["sociallinks"] = {}

        # Fetch business_info
        cursor.execute("SELECT * FROM business_info WHERE user_id_fk = %s", (user_id,))
        business_info = cursor.fetchone()

        if business_info:
            # Parse JSON fields
            for field in ["PaymentMethods", "PaymentDetails", "ecommerce_data"]:
                if business_info.get(field):
                    try:
                        business_info[field] = json.loads(business_info[field])
                    except Exception:
                        pass

            # ✅ Add signed URL for ProofOfBusinessFile
            proof_file_key = business_info.get("ProofOfBusinessFile")
            if proof_file_key:
                business_info["ProofOfBusinessFile"] = attach_CLDFRNT_url(
                    proof_file_key
                )

            # ✅ Add signed URL for BusinessImage
            image_key = business_info.get("BusinessImage")
            if image_key:
                business_info["BusinessImage"] = attach_CLDFRNT_url(image_key)

        cursor.close()
        conn.close()

        return (
            jsonify(
                {"status": "success", "user": user, "business_info": business_info}
            ),
            200,
        )

    except Exception as e:
        # print("Error in get_onboarding:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@users_bp.route("/onboaring_update", methods=["POST"])
def onboarding_update():
    try:
        user_id = request.form.get("user_id") or session.get("user_id")
        data = request.form.get("data")
        if data:
            data = json.loads(data)
        else:
            data = {}
        business_proof = request.files.get("businessProof")
        business_Image = request.files.get("businessImage")

        if business_proof:
            filename = secure_filename(business_proof.filename)
            temp_path = os.path.join("/tmp", filename)
            business_proof.save(temp_path)

            val = upload_any_file(
                file_path=temp_path,
                user_id=user_id,
                type="user",
                file_name="businessproof_" + filename,
            )
            business_proof_paths = val["s3_key"]

        if business_Image:
            filename = secure_filename(business_Image.filename)
            temp_path = os.path.join("/tmp", filename)
            business_Image.save(temp_path)

            val = upload_any_file(
                file_path=temp_path,
                user_id=user_id,
                type="user",
                file_name="businessimage_" + filename,
            )
            business_Image_paths = val["s3_key"]

        if not user_id:
            return jsonify({"status": "error", "message": "user_id is required"}), 400

        conn = connect_to_rds()
        cursor = conn.cursor()

        # --- USER TABLE UPDATE ---
        user_fields = {
            "first_name": data.get("name"),
            # "email": data.get("primaryEmail"),
            "phone": data.get("primaryPhone"),
        }

        social_keys = {
            "whatsapp": data.get("whatsappNumber"),
            "facebook": data.get("facebookId"),
            "instagram": data.get("instagramId"),
            "linkedin": data.get("linkedinId"),
            "slack": data.get("slackId"),
            "teams": data.get("teamsId"),
            "shopify": data.get("shopifyId"),
            "woocommerce": data.get("woocommerceUrl"),
        }
        sociallinks = {k: v for k, v in social_keys.items() if v}

        set_user_clauses = []
        user_values = []

        for col, val in user_fields.items():
            if val is not None:
                set_user_clauses.append(f"{col} = %s")
                user_values.append(val)

        if sociallinks:
            set_user_clauses.append("sociallinks = %s")
            user_values.append(json.dumps(sociallinks))

        if set_user_clauses:
            set_user_clauses.append("updated_in = %s")
            user_values.append(datetime.utcnow())
            user_values.append(user_id)

            cursor.execute(
                f"""
                UPDATE users SET {', '.join(set_user_clauses)}
                WHERE user_id = %s
            """,
                tuple(user_values),
            )

        # --- BUSINESS_INFO TABLE UPDATE ---
        cursor.execute(
            "SELECT business_info_id FROM business_info WHERE user_id_fk = %s",
            (user_id,),
        )
        if cursor.fetchone():
            # Only update fields present in data
            business_fields = {
                "BusinessName": data.get("businessName"),
                "Age": str(data["age"]) if "age" in data else None,
                "Sex": data["cognitype"].capitalize() if "cognitype" in data else None,
                "LineOfBusiness": data.get("lineOfBusiness"),
                "YearsInBusiness": (
                    str(data["yearsInBusiness"]) if "yearsInBusiness" in data else None
                ),
                "HasLicense": data.get("hasLicense"),
                "RegistrationStatus": (
                    "Registered"
                    if data.get("registrationStatus") == "registered"
                    else ("Non-Registered" if "registrationStatus" in data else None)
                ),
                "ProofOfBusinessFile": (
                    str(business_proof_paths)
                    if request.files.get("businessProof")
                    else None
                ),
                "RegistrationNumber": data.get("registrationNumber"),
                "GSTNumber": data.get("gstNumber"),
                "Country": data.get("country"),
                "ProvinceOrState": data.get("province"),
                "City": data.get("city"),
                "BusinessImage": (
                    str(business_Image_paths)
                    if request.files.get("businessImage")
                    else None
                ),
                "BusinessEmail": data.get("businessEmail"),
                "PaymentMethods": (
                    json.dumps(data.get("paymentMethods"))
                    if "paymentMethods" in data
                    else None
                ),
                "PaymentDetails": (
                    json.dumps(data.get("paymentDetails"))
                    if "paymentDetails" in data
                    else None
                ),
                "OwnershipType": data.get("ownershipType"),
                "BusinessTimings": data.get("businessTimings"),
                "WebsiteUrl": data.get("websiteUrl"),
                "SecondaryPhone": data.get("secondaryPhone"),
                "GSTNotAvailable": data.get("gstNotAvailable"),
                "SameAsBilling": data.get("sameAsBilling"),
                "businessLocation": data.get("businessLocation"),
            }

            # Conditionally add address fields
            if any(
                k in data for k in ["doorNumber", "unitNumber", "streetName", "zipCode"]
            ):
                billing_address = f"{data.get('doorNumber', '')}, {data.get('unitNumber', '')}, {data.get('streetName', '')}, {data.get('zipCode', '')}"
                business_fields["BillingAddress"] = billing_address

            if any(
                k in data
                for k in [
                    "shippingDoorNumber",
                    "shippingUnitNumber",
                    "shippingStreetName",
                    "shippingZipCode",
                ]
            ):
                shipping_address = f"{data.get('shippingDoorNumber', '')}, {data.get('shippingUnitNumber', '')}, {data.get('shippingStreetName', '')}, {data.get('shippingZipCode', '')}"
                business_fields["ShippingAddress"] = shipping_address

            # Build dynamic SQL
            set_business_clauses = []
            business_values = []

            for col, val in business_fields.items():
                if val is not None:
                    set_business_clauses.append(f"{col} = %s")
                    business_values.append(val)

            if set_business_clauses:
                business_values.append(user_id)
                cursor.execute(
                    f"""
                    UPDATE business_info SET {', '.join(set_business_clauses)}
                    WHERE user_id_fk = %s
                """,
                    tuple(business_values),
                )

        conn.commit()
        cursor.close()
        conn.close()

        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Onboarding data updated successfully.",
                }
            ),
            200,
        )

    except Exception as e:
        # print("Error in onboarding_update:", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@users_bp.route("/generate-website-api-key", methods=["POST"])
def generate_api_key():
    data = request.get_json()
    # print(session.get("user", "No user in session"))
    user_id = data.get("userid") or session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401
    new_api_key = uuid.uuid4()
    # connection = pymysql.connect(
    #        host='database-1.czoeckiiosd2.ap-south-1.rds.amazonaws.com',
    #         user='skilbyt_db',
    #          password='JesusChristIsLord$1',
    #           db='ai_support'
    #          )
    connection = connect_to_rds()
    try:
        with connection.cursor() as cursor:
            sql = "SELECT 1 FROM launch WHERE user_id_fk = %s LIMIT 1"
            cursor.execute(sql, (user_id,))
            result = cursor.fetchone()
            if not result:
                sub_agent_id = uuid.uuid4()
                launch_id = uuid.uuid4()

                subagent_sql = """
                INSERT INTO subagents (
                sub_agent_id, launch_id_fk, name, description,voice_type,
                documentation_link, model_version, created_at, updated_at
                ) VALUES (%s, %s, %s,NULL, NULL, NULL, NULL, NULL, NULL)
                """
                cursor.execute(subagent_sql, (sub_agent_id, None, ""))

                insert_sql = """
                    INSERT INTO launch (launch_id, sub_agent_id_fk, user_id_fk, api_id, website_name)
                    VALUES (%s, %s, %s, %s, %s)
                """

                cursor.execute(
                    insert_sql, (launch_id, sub_agent_id, user_id, new_api_key, None)
                )
                cursor.execute(
                    """
                UPDATE subagents
                SET launch_id_fk = %s
                WHERE sub_agent_id = %s
                """,
                    (launch_id, sub_agent_id),
                )

                connection.commit()
                return jsonify({"apiKey": new_api_key}), 200

            else:
                sql = "SELECT api_id FROM launch WHERE user_id_fk = %s LIMIT 1"
                cursor.execute(sql, (user_id,))
                result = cursor.fetchone()

                if (
                    result and result[0]
                ):  # ✅ Check if result exists AND api_id is not None
                    return jsonify({"apiKey": result[0]}), 200
                else:
                    update_sql = """
                        UPDATE launch
                        SET api_id = %s
                        WHERE user_id_fk = %s
                    """
                    cursor.execute(update_sql, (new_api_key, user_id))

                connection.commit()
                return jsonify({"apiKey": new_api_key}), 200
    except Exception as e:
        # print(f"Error generating API key: {e}")  # Or use logging instead of print
        return jsonify({"error": f"Internal server error {e}"}), 500


@users_bp.route("/get_leads", methods=["GET"])
def get_leads_route():
    try:
        conn = connect_to_rds()
        with conn.cursor() as cursor:
            sql = "SELECT * FROM leads ORDER BY id DESC"
            cursor.execute(sql)
            leads = cursor.fetchall()
        conn.close()

        # Transform the data to match the expected format in the frontend
        formatted_leads = []
        for lead in leads:
            formatted_leads.append(
                {
                    "id": lead[0],
                    "name": lead[1],
                    "company": lead[2] if lead[2] else "",
                    "email": lead[3],
                    "phone": lead[4] if lead[4] else "",
                    "status": lead[5],
                }
            )

        return jsonify({"leads": formatted_leads})
    except pymysql.Error as err:
        logger.error(f"Database error: {err}")
        return jsonify({"error": f"Database error: {err}"}), 500
    except Exception as e:
        logger.error(f"An unexpected error occurred: {str(e)}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500


@users_bp.route("/add_lead", methods=["POST"])
def add_lead_route():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    lead_name = data.get("lead_name")
    company = data.get("company")
    email = data.get("email")
    phone = data.get("phone")
    status = data.get("status")

    if not lead_name or not email or not status:
        return (
            jsonify({"error": "Missing required fields: lead_name, email, status"}),
            400,
        )

    try:
        # conn = get_db_connection()
        conn = connect_to_rds()
        with conn.cursor() as cursor:
            sql = "INSERT INTO leads (lead_name, company, email, phone, status) VALUES (%s, %s, %s, %s, %s)"
            cursor.execute(sql, (lead_name, company, email, phone, status))
            conn.commit()
            lead_id = cursor.lastrowid
        conn.close()

        return (
            jsonify(
                {
                    "message": "Lead added successfully to DB",
                    "lead": {
                        "id": lead_id,
                        "lead_name": lead_name,
                        "company": company,
                        "email": email,
                        "phone": phone,
                        "status": status,
                    },
                }
            ),
            201,
        )
    except pymysql.Error as err:
        logger.error(f"Database error: {err}")
        return jsonify({"error": f"Database error: {err}"}), 500
    except Exception as e:
        logger.error(f"An unexpected error occurred: {str(e)}")
        return jsonify({"error": f"An unexpected error occurred: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()


@users_bp.route("/get_user_permissions/<userid>", methods=["GET"])
def get_user_permissions(userid):
    """Get all roles and invited users for a user"""
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT user_type, permissions FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "User not found"}), 404

        if row["user_type"] != "":
            return jsonify({"permissions": "ALL"}), 200

        # Non-admin → parse JSON
        permissions = {}
        if row.get("permissions"):
            try:
                permissions = json.loads(row["permissions"])
            except Exception:
                permissions = {}

        role_permissions = permissions.get("role", {})
        role_permissions["status"] = permissions.get("status")

        return jsonify({"permissions": role_permissions}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/get_account_info/<userid>", methods=["GET"])
def get_account_info(userid):
    try:
        conn = connect_to_rds()
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT 
                    u.user_id,
                    u.first_name, 
                    u.last_name, 
                    u.email, 
                    l.api_id 
                FROM users u
                LEFT JOIN launch l 
                    ON l.user_id_fk = u.user_id  
                WHERE u.user_id = %s
                """,
                (userid,),
            )
            row = cursor.fetchone()
        conn.close()

        if not row:
            return jsonify({"error": "User not found"}), 404

        # Handle None gracefully
        return (
            jsonify(
                {
                    "user_id": row.get("user_id"),
                    "first_name": row.get("first_name") or "",
                    "last_name": row.get("last_name") or "",
                    "email": row.get("email") or None,
                    "api_key": row.get("api_id") or None,
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500
#Email existance check
@users_bp.route("/email_exist/<path:email>",methods=["GET"])
def email_exist(email):
    # data = request.get_json()
    # email = data.get("email")
    if not email:
        return jsonify({"error":"Email is required"}),400
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE email=%s",(email,))
        user = cursor.fetchone()
        if not user:
            return jsonify({"emailExist": bool(user)}),200
        password = user["password_hash"]
        return jsonify({"emailExist": bool(user),
                        "passwordExist":bool(password)}),200

    except Exception as e:
        return jsonify({"error":str(e)}),500
#creating new user
@users_bp.route("/create_new_user", methods=["POST"])
def create_new_user():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400

    first_name = data.get("first_name")
    last_name = data.get("last_name")
    email = data.get("email")
    password = data.get("password")
    phone = data.get("phone")
    location = data.get("location")
    password_pattern = r"^(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$"
    email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    email_verified = False

    if not email or not password or not location:
        return (
            jsonify({"error": "Missing required fields : email, password, location"}),
            400,
        )

    if not re.fullmatch(password_pattern, password):
        return jsonify({"message": "Password does not meets the requirement"}), 401
    if not re.fullmatch(email_pattern, email):
        return jsonify({"message": "Invalid mail format"}), 401

    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT user_id,user_type FROM users WHERE email = %s", (email,))
        user_exists = cursor.fetchone()

        if user_exists:
            logger.info("User already exist with this email address")
            return jsonify({"message": "User already exists. Please login"}), 401
        logger.info("creating a new user")
        # Get value for social based on email domain
        provider_domains = {
            "Google": {"gmail.com", "googlemail.com", "google.com"},
            "Microsoft": {"outlook.com", "hotmail.com", "live.com"},
            "Zoho": {"zoho.com", "zohomail.com"},
        }
        social = ""
        domain = email.split("@")[-1].lower()
        for providers, domains in provider_domains.items():
            if domain in domains:
                social = providers

        social = social or "Custom"
        # hash the password
        hashed_password = generate_password_hash(password)
        # generate user_id
        user_id = str(uuid.uuid4().hex)
        # Generate verification url
        verify_url = generate_hashed_url(
            base_url=f"{os.getenv('BASE_FRNT_URL')}/login",
            invited_to=email,
            invited_by=os.getenv("TEST_EMAIL2"),
        )

        token = verify_url.rstrip("/").split("/")[-1]
        send_email_link(email, verify_url)

        cursor.execute(
            """INSERT INTO users(user_id,user_type,launch_id_fk, first_name, last_name, email,phone,
                location,social,password_hash,created_in)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,Now())                
                """,
            (
                user_id,
                "user",
                "",
                first_name,
                last_name,
                email,
                phone,
                location,
                social,
                hashed_password,
            ),
        )

        conn.commit()
        conn.close()
        logger.info("New user created")
        return (
            jsonify(
                {"message": "New user created successfully",
                 "user_id":f"{user_id}"}
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Email sending method
def send_email_link(email, verify_url):

    html = f"""<p>Click the link below to verify your email:</p>
                <p><b>{verify_url}</b></p>
                <p>The link will expire in 1 hour</p>"""
    gmail_service = GmailService("109161866299858012556")
    gmail_service.send_email(
        receipent_emails=email,
        subject="Verification for new user creation",
        body_text=html,
    )

#verification of email
@users_bp.route("/verify_email",methods=["POST"])
def verify_email():
    data = request.get_json()
    token = data.get("token")
    try:
        decrypted = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry_time = decrypted.split("|")
        if int(expiry_time) < int(time.time()):
            return jsonify({
                "valid": False,
                "error": "Verify link has expired"
            }), 400
        
        return jsonify({
            "emailVerified":True,
            "email":invited_to
        }),200

    except Exception as e:
        return jsonify({"error":str(e)}),500


#user sign in method
@users_bp.route("/user_login", methods=["POST"])
def user_login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        if not email or not password:
            return jsonify({"error": "Email and password are required"}), 401

        query = "SELECT * FROM users WHERE email=%s"
        cursor.execute(query, (email))
        user = cursor.fetchone()
        

        if not user:
            return jsonify({"error": "Incorrect email address"}), 401
        
        password_hash = user["password_hash"]
        if not check_password_hash(password_hash, password):
            return jsonify({"error": "Incorrect password"}), 401

        # onboarding check
        newuser = check_onboarding_user(user["user_id"])
        logger.info("new user %s", newuser)

        response = jsonify(
            {
                "message": "Login successful",
                "user": {
                    "user_id": user["user_id"],
                    "email": user["email"],
                    "first_name": user["first_name"],
                    "last_name": user["last_name"],
                    "user_type": user["user_type"],
                },
                "betaAgreementAccepted": newuser,
            }
        )
        conn.close()
        return response, 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# update the user_type based on user_id
@users_bp.route("/update_user_type", methods=["POST"])
def update_user_type():
    data = request.get_json()
    user_id = data.get("user_id")
    user_type = data.get("user_type")
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if not user_id or not user_type:
            return (
                jsonify({"error": "Missing required fields : user_id,user_type"}),
                400,
            )

        query = "UPDATE users SET user_type = %s,updated_in = Now() where user_id = %s "
        cursor.execute(
            query,
            (user_type, user_id),
        )
        conn.commit()
        conn.close()
        return jsonify({"message": "User type updated successsfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# update old password
@users_bp.route("/update_password", methods=["POST"])
def update_password():
    data = request.get_json()
    user_id = data.get("user_id")
    oldPassword = data.get("oldPassword")
    newPassword = data.get("newPassword")
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if not user_id or not oldPassword or not newPassword:
            return (
                jsonify(
                    {
                        "error": "Missing required fields : user_id, OldPassword, NewPassword"
                    }
                ),
                400,
            )
        cursor.execute(
            """SELECT password_hash FROM users WHERE user_id = %s""", (user_id)
        )
        row = cursor.fetchone()
        password_hash = row["password_hash"]

        if not check_password_hash(password_hash, oldPassword):
            return jsonify({"message": "Old password is incorrect"}), 400

        new_hashed_password = generate_password_hash(newPassword)

        query = (
            "UPDATE users SET password_hash = %s,updated_in = NOW() WHERE user_id = %s"
        )
        cursor.execute(
            query,
            (new_hashed_password, user_id),
        )

        conn.commit()
        conn.close()
        return jsonify({"message": "Password updated successsfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# update first name and last name of the user
@users_bp.route("/update_name", methods=["POST"])
def update_name():
    data = request.get_json()
    user_id = data.get("user_id")
    first_name = data.get("first_name")
    last_name = data.get("last_name")
    if not user_id:
        return (
            jsonify({"error": "Missing required fields : user_id"}),
            400,
        )
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        query = "UPDATE users SET first_name = %s, last_name = %s, updated_in = NOW() WHERE user_id = %s"
        cursor.execute(query, (first_name, last_name, user_id))

        conn.commit()
        conn.close()
        return jsonify({"message": "User name updated successsfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

#Logic for forgot password
@users_bp.route("/forgot_password", methods=["POST"])
def forgot_password():
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"error": "Email is required"}), 401
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT user_id FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if not user:
            return jsonify({"error": "User with this email is not exists"})

        reset_url = generate_hashed_url(base_url=f"{os.getenv('BASE_FRNT_URL')}/ResetPassword",
            invited_to=email,
            invited_by=os.getenv("TEST_EMAIL2"),)

        send_password_reset_email(email,reset_url)
        return jsonify({"message":"Reset link sent to email"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
#method to send reset link
def send_password_reset_email(email,reset_url):
    html = f"""
    <p>You requested a password reset.</p>
    <p>Click the link below to reset your password:</p>
    <p><b>{reset_url}</b></p>
    <p>This link will expire in 1 hour.</p>
    """

    gmail_service = GmailService("109161866299858012556")
    gmail_service.send_email(
        receipent_emails=email,
        subject="Reset your password",
        body_text=html,
    )

#validation of reset link
@users_bp.route("/validateResetToken",methods=["POST"])
def validate_reset_token():
    data = request.get_json()
    token = data.get("token")
    try:
        decrypted = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry_time = decrypted.split("|")
        if int(expiry_time) < int(time.time()):
            return jsonify({
                "valid": False,
                "error": "Reset link has expired"
            }), 400
        
        return jsonify({
            "valid":True,
            "email":invited_to
        }),200

    except Exception as e:
        return jsonify({"error":str(e)}),500
    
#Updating the new password through forgot password
@users_bp.route("/reset_password",methods=["POST"])
def reset_password():
    data = request.get_json()
    email = data.get("email")
    newPassword = data.get("newPassword")
    password_pattern = r'^(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$'

    if not newPassword:
        return jsonify({"error":"New password is required"}),401
    
    if not re.match(password_pattern,newPassword):
        return jsonify({"error":"Password does not meet the requirement. Should contain atleast 8 characters,one uppercase letter,one numeric character and one special character",
                        }),401
    try:
        new_hashed_password = generate_password_hash(newPassword)
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT * FROM users WHERE email = %s",(email,))
        user = cursor.fetchone()

        if not user:
            logger.warning("User not found with this email")
            return jsonify({"error":"User not found"})

        logger.info("Updating the new hashed password through reset link")
        cursor.execute("""UPDATE users
                    SET password_hash = %s, updated_in = NOW() where email=%s""",
                    (new_hashed_password,email,))
        
        conn.commit()
        conn.close()
        return jsonify({"message":"Password reset successfully"})
    except Exception as e:
        return jsonify({"error":str(e)}),500