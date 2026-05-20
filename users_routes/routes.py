import secrets
import traceback
from urllib.parse import urlencode

from flask import Blueprint, request, jsonify, session, g
import pymysql
import os
import uuid
from db.rds_db import connect_to_rds
import json
from datetime import datetime
from microsoft_route.routes import get_microsoft_redirect_uri
from utils.base_logger import get_logger
from utils.permission_required import permission_required_body
from werkzeug.utils import secure_filename
from utils.s3_utils import attach_CLDFRNT_url, generate_presigned_url, upload_any_file
from werkzeug.security import generate_password_hash, check_password_hash
import re
from dotenv import load_dotenv
from invited_users.uszr_helper import generate_hashed_url
from services.gmail_service import GmailService
from services.totp_service import TOTPService
from db.db_checkers import (
    check_onboarding_user,
    delete_user_domain,
    fetch_user_domains,
    get_email_by_id,
)
from services.audit_log_service import (
    log_audit_event,
    OAUTH_TOKEN_STORED,
    API_KEY_CREATED,
)
from cryptography.fernet import Fernet
import base64
import time
from utils.g_scopes import g_basescopes
from google_auth_oauthlib.flow import Flow
from msal import ConfidentialClientApplication
import requests
import dns.resolver
from utils.key_rotation_manager import SecureKMSService
from db.db_checkers import make_api_key

users_bp = Blueprint("users", __name__)

logger = get_logger(__name__)
from services.audit_log_service import (
    log_audit_event,
    LOGIN_SUCCESS,
    LOGIN_FAILED,
    TOTP_SETUP,
    TOTP_VERIFIED,
    PASSWORD_CHANGED,
    PASSWORD_RESET,
    USER_TYPE_CHANGED,
    ENCRYPTION_KEY_ROTATED,
    DOMAIN_ADDED,
    DOMAIN_DELETED,
    USER_CREATED,
)

SECRET_KEY = os.getenv("SECRETKEY")
fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY.encode("utf-8").ljust(32)[:32]))

M_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID")
M_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET")
M_TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID")
M_AUTHORITY = f"https://login.microsoftonline.com/{M_TENANT_ID}"
M_SCOPES = [
    "offline_access",
    "User.Read",
    "Mail.Send",
    "Mail.ReadWrite",
    "Calendars.ReadWrite",
    "OnlineMeetings.ReadWrite",
    "Chat.ReadWrite",
    "Files.Read.All",
]


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
@permission_required_body("kb.api.regenerate")
def generate_api_key():
    data = request.get_json()
    # print(session.get("user", "No user in session"))
    user_id = data.get("userid") or session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    result = make_api_key(user_id=user_id)

    # Audit logging
    actor_email = get_email_by_id(user_id)
    is_success = isinstance(result, tuple) and result[1] == 201
    log_audit_event(
        action=API_KEY_CREATED,
        endpoint="/generate-website-api-key",
        ip=request.remote_addr,
        status="success" if is_success else "failure",
        actor_user_id=user_id,
        actor_email=actor_email,
        metadata={"key_prefix": "***"},
    )
    g.audit_logged = True

    return result


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
                "SELECT user_type, permissions,special_access FROM users WHERE user_id=%s",
                (userid,),
            )
            row = cursor.fetchone()

        conn.close()

        if not row:
            return jsonify({"error": "User not found"}), 404

        if row["user_type"] in ("admin", "superadmin") or row["special_access"]:
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
                    u.user_type, 
                    u.email,
                    u.social,
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
                    "social": row.get("social"),
                    "user_type": row.get("user_type"),
                }
            ),
            200,
        )

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Email existance check
@users_bp.route("/email_exist/<path:email>", methods=["GET"])
def email_exist(email):
    # data = request.get_json()
    # email = data.get("email")
    if not email:
        return jsonify({"error": "Email is required"}), 400
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()
        if not user:
            return jsonify({"emailExist": bool(user)}), 200
        password = user["password_hash"]
        return jsonify({"emailExist": bool(user), "passwordExist": bool(password)}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# creating new user
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
        return jsonify({"message": "Password does not meets the requirement"}), 400
    if not re.fullmatch(email_pattern, email):
        return jsonify({"message": "Invalid mail format"}), 400

    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT user_id,user_type FROM users WHERE email = %s", (email,))
        user_exists = cursor.fetchone()

        if user_exists:
            logger.info("User already exist with this email address")
            return jsonify({"message": "User already exists. Please login"}), 400
        logger.info("creating a new user")
        # Get value for domain based from email
        user_domain = ""

        if email and "@" in email:
            user_domain = email.split("@")[-1].lower()

        domain_data = {"primary": user_domain, "secondary": []}  # empty initially
        # hash the password
        hashed_password = generate_password_hash(password)
        # generate user_id
        user_id = str(uuid.uuid4().hex)

        # send_email_link(email, verify_url)

        cursor.execute(
            """INSERT INTO users(user_id,user_type,launch_id_fk, first_name, last_name, email,phone,
                location,domain,password_hash,created_in)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,Now())                
                """,
            (
                user_id,
                "admin",
                "",
                first_name,
                last_name,
                email,
                phone,
                location,
                json.dumps(domain_data),
                hashed_password,
            ),
        )

        conn.commit()
        conn.close()
        logger.info("New user created")

        actor_user_id = data.get("user_id")
        actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
        log_audit_event(
            action=USER_CREATED,
            endpoint="/create_new_user",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            target_user_id=user_id,
            target_email=email,
            metadata={"user_type": "admin"},
        )
        g.audit_logged = True

        return (
            jsonify(
                {"message": "New user created successfully", "user_id": f"{user_id}"}
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# Email sending method
@users_bp.route("/send_email_link", methods=["POST"])
def send_email_link():
    data = request.get_json()
    email = data.get("email")
    if not email:
        logger.error("Email is required")
        return jsonify({"error": "Email is required"}), 400
    try:
        # Generate verification url
        verify_url = generate_hashed_url(
            base_url=f"{os.getenv('BASE_FRNT_URL')}/verify-email",
            invited_to=email,
            invited_by=os.getenv("TEST_EMAIL2"),
        )
        html = f"""<p>Click the link below to verify your email:</p>
                    <p><a href="{verify_url}">Verify Email</a></p>
                    <p>The link will expire in 1 hour</p>"""
        gmail_service = GmailService("109161866299858012556")
        # send email
        result = gmail_service.send_email(
            receipent_emails=email,
            subject="Verification for new user creation",
            body_text=html,
        )
        if isinstance(result, dict) and result.get("success") is False:
            raise Exception(result.get("error", "Failed to send verification email"))
        logger.info("Email sent successfully")
        return jsonify({"message": "Email sent successfully"}), 200
    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# verification of email
@users_bp.route("/verify_email", methods=["POST"])
def verify_email():
    data = request.get_json()
    token = data.get("token")
    try:
        decrypted = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry_time = decrypted.split("|")
        if int(expiry_time) < int(time.time()):
            logger.error("Email verification link expired")
            return jsonify({"valid": False, "error": "Verify link has expired"}), 400
        logger.info("Email verifies successfully")
        return jsonify({"emailVerified": True, "email": invited_to}), 200

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# user sign in method
@users_bp.route("/user_login", methods=["POST"])
def user_login():
    data = request.get_json()
    email = data.get("email")
    password = data.get("password")

    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        if not email or not password:
            logger.error("Email and password are required")
            return jsonify({"error": "Email and password are required"}), 400

        query = "SELECT * FROM users WHERE email=%s"
        cursor.execute(query, (email))
        user = cursor.fetchone()

        if not user:
            logger.error("Incorrect email address")
            log_audit_event(
                action=LOGIN_FAILED,
                endpoint="/user_login",
                ip=request.remote_addr,
                status="failure",
                actor_email=email,
                metadata={"reason": "unknown_email"},
            )
            g.audit_logged = True
            return jsonify({"error": "Incorrect email address"}), 400

        password_hash = user["password_hash"]
        if not check_password_hash(password_hash, password):
            logger.error("Incorrect password")
            log_audit_event(
                action=LOGIN_FAILED,
                endpoint="/user_login",
                ip=request.remote_addr,
                status="failure",
                actor_user_id=user["user_id"],
                actor_email=user["email"],
                metadata={"reason": "wrong_password"},
            )
            g.audit_logged = True
            return jsonify({"error": "Incorrect password"}), 400

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
                "has_totp": bool(user["totp_secret"]),
            }
        )
        conn.close()
        logger.info("Login successfull")
        session["user_id"] = user["user_id"]
        session.pop("active_workspace_id", None)
        log_audit_event(
            action=LOGIN_SUCCESS,
            endpoint="/user_login",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user["user_id"],
            actor_email=user["email"],
        )
        g.audit_logged = True
        return response, 200
    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# TOTP setup
@users_bp.route("/totp_setup", methods=["POST"])
def totp_setup():
    data = request.get_json()
    user_id = data.get("user_id")
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        user = cursor.fetchone()

        if not user.get("totp_secret"):
            logger.info("Generating TOTP secret")
            secret = TOTPService.generate_secret()
            cursor.execute(
                "UPDATE users SET totp_secret = %s WHERE user_id = %s",
                (secret, user_id),
            )
            conn.commit()
            log_audit_event(
                action=TOTP_SETUP,
                endpoint="/totp_setup",
                ip=request.remote_addr,
                status="success",
                actor_user_id=user_id,
                actor_email=user["email"],
            )
            g.audit_logged = True
            user["totp_secret"] = secret
        email = user["email"]
        # secret = user["totp_secret"]
        uri = TOTPService.provisioning_uri(user["totp_secret"], user_id, email)

        conn.close()
        logger.info("TOTP URI sent for verification")
        return (
            jsonify(
                {
                    "totp_uri": uri,
                    "totp_secret": user["totp_secret"],
                    "is_totp_enabled": bool(user["totp_secret"]),
                }
            ),
            200,
        )

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# TOtP verify
@users_bp.route("/totp_verify", methods=["POST"])
def totp_verify():
    data = request.get_json()
    user_id = data.get("user_id")
    code = data.get("code")
    if not code:
        logger.error("Code is required")
        return jsonify({"error": "Code is required "}), 400
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        user = cursor.fetchone()
        totp_secret = user["totp_secret"]
        email = user["email"]
        code = str(code).strip()
        if not totp_secret:
            logger.error("TOTP secret is required")
            return jsonify({"error": "TOTP secret is required"}), 400
        logger.info("Verifying TOTP secret")
        verify = TOTPService.verify_totp(totp_secret, code)
        if not verify:
            logger.error("TOTP verification failed")
            return jsonify({"error": "TOTP not verified"}), 400
        logger.info("TOTP verified successfully")
        log_audit_event(
            action=TOTP_VERIFIED,
            endpoint="/totp_verify",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=email,
        )
        g.audit_logged = True
        return jsonify({"message": "TOTP verified successfully", "verified": True}), 200
    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# update the user_type based on user_id
@users_bp.route("/update_user_type", methods=["POST"])
def update_user_type():
    # SECURITY PATCH: Require session auth + admin-only access
    current_user_id = session.get("user_id")
    if not current_user_id:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json()
    user_id = data.get("user_id")
    user_type = data.get("user_type")
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        if not user_id or not user_type:
            logger.error("Missing required fields : user_id and user_type")
            return (
                jsonify({"error": "Missing required fields : user_id,user_type"}),
                400,
            )

        # SECURITY PATCH: Verify current user is admin
        cursor.execute(
            "SELECT user_type FROM users WHERE user_id = %s", (current_user_id,)
        )
        current_user = cursor.fetchone()
        if not current_user or current_user["user_type"] != "admin":
            return jsonify({"error": "Admin access required"}), 403

        logger.info("Updating the user_type")
        query = "UPDATE users SET user_type = %s,updated_in = Now() where user_id = %s "
        cursor.execute(
            query,
            (user_type, user_id),
        )
        conn.commit()
        actor_user_id = current_user_id
        actor_email = get_email_by_id(actor_user_id)
        log_audit_event(
            action=USER_TYPE_CHANGED,
            endpoint="/update_user_type",
            ip=request.remote_addr,
            status="success",
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            metadata={"new_user_type": user_type, "target_user_id": user_id},
        )
        g.audit_logged = True
        conn.close()
        logger.info("user_type updated successfully")
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
    if not user_id or not oldPassword or not newPassword:
        return (
            jsonify(
                {"error": "Missing required fields : user_id, OldPassword, NewPassword"}
            ),
            400,
        )
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute(
            """SELECT password_hash FROM users WHERE user_id = %s""", (user_id)
        )
        row = cursor.fetchone()
        password_hash = row["password_hash"]

        if not check_password_hash(password_hash, oldPassword):
            return jsonify({"message": "Old password is incorrect"}), 400

        new_hashed_password = generate_password_hash(newPassword)
        logger.info("Updating new password")
        query = (
            "UPDATE users SET password_hash = %s,updated_in = NOW() WHERE user_id = %s"
        )
        cursor.execute(
            query,
            (new_hashed_password, user_id),
        )

        conn.commit()
        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=PASSWORD_CHANGED,
            endpoint="/update_password",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
        )
        g.audit_logged = True
        conn.close()
        logger.info("New password updated successfully")
        return jsonify({"message": "Password updated successsfully"}), 200

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
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
        logger.info("First name and last name are updated successfully")
        return jsonify({"message": "User name updated successsfully"}), 200

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# Logic for forgot password
@users_bp.route("/forgot_password", methods=["POST"])
def forgot_password():
    data = request.get_json()
    email = data.get("email")
    if not email:
        return jsonify({"error": "Email is required"}), 400
    try:
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT user_id FROM users WHERE email=%s", (email,))
        user = cursor.fetchone()

        if not user:
            logger.error("User with this email is not exists")
            return jsonify({"error": "User with this email is not exists"})

        expiry_time = int(time.time()) + 3600
        payload = f"{os.getenv('TEST_EMAIL2')}|{email}|{expiry_time}"
        token = fernet.encrypt(payload.encode()).decode()
        reset_url = f"{os.getenv('BASE_FRNT_URL')}/ResetPassword/{token}"

        send_password_reset_email(email, reset_url)
        logger.info("Reset link sent to email")
        return jsonify({"message": "Reset link sent to email"})

    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


# method to send reset link
def send_password_reset_email(email, reset_url):
    html = f"""
    <p>You requested a password reset.</p>
    <p>Click the link below to reset your password:</p>
    <p><b>{reset_url}</b></p>
    <p>This link will expire in 1 hour.</p>
    """

    gmail_service = GmailService("109161866299858012556")
    result = gmail_service.send_email(
        receipent_emails=email,
        subject="Reset your password",
        body_text=html,
    )
    if isinstance(result, dict) and result.get("success") is False:
        raise Exception(result.get("error", "Failed to send password reset email"))


# validation of reset link
@users_bp.route("/validateResetToken", methods=["POST"])
def validate_reset_token():
    data = request.get_json()
    token = data.get("token")
    try:
        decrypted = fernet.decrypt(token.encode()).decode()
        invited_by, invited_to, expiry_time = decrypted.split("|")
        if int(expiry_time) < int(time.time()):
            return jsonify({"valid": False, "error": "Reset link has expired"}), 400

        return jsonify({"valid": True, "email": invited_to}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Updating the new password through forgot password
@users_bp.route("/reset_password", methods=["POST"])
def reset_password():
    data = request.get_json()
    email = data.get("email")
    newPassword = data.get("newPassword")
    password_pattern = r"^(?=.*[A-Z])(?=.*\d)(?=.*[^A-Za-z0-9]).{8,}$"

    if not newPassword:
        logger.error("New password is required")
        return jsonify({"error": "New password is required"}), 400

    if not re.match(password_pattern, newPassword):
        logger.error("password does not meet the basic re")
        return (
            jsonify(
                {
                    "error": "Password does not meet the requirement. Should contain atleast 8 characters,one uppercase letter,one numeric character and one special character",
                }
            ),
            400,
        )
    try:
        new_hashed_password = generate_password_hash(newPassword)
        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()

        if not user:
            logger.warning("User not found with this email")
            return jsonify({"error": "User not found"})

        logger.info("Updating the new hashed password through reset link")
        cursor.execute(
            """UPDATE users
                    SET password_hash = %s, updated_in = NOW() where email=%s""",
            (
                new_hashed_password,
                email,
            ),
        )

        conn.commit()
        log_audit_event(
            action=PASSWORD_RESET,
            endpoint="/reset_password",
            ip=request.remote_addr,
            status="success",
            actor_email=email,
            metadata={"method": "forgot_password_flow"},
        )
        g.audit_logged = True
        conn.close()
        logger.info("Password reset successfully")
        return jsonify({"message": "Password reset successfully"})
    except Exception as e:
        logger.error(f"Unexpected error occured : {str(e)}")
        return jsonify({"error": str(e)}), 500


@users_bp.route("/connect/<provider>", methods=["GET"])
def connect_provider(provider):
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Unauthorized"}), 401

    if provider not in ["google", "microsoft"]:
        return jsonify({"error": "Unsupported provider"}), 400

    session["oauth_provider"] = provider

    redirect_uri = f"{os.getenv('BASE_FRNT_URL')}/oauth/connect/callback"

    if provider == "google":
        flow = Flow.from_client_secrets_file(
            "client_secrets.json",
            scopes=g_basescopes,
            redirect_uri=redirect_uri,
        )

        auth_url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )

    elif provider == "microsoft":
        auth_url, state = build_microsoft_auth_url()
        session["oauth_state"] = state
        session["oauth_provider"] = "microsoft"
        return jsonify({"auth_url": auth_url})

    session["oauth_state"] = state

    return jsonify({"auth_url": auth_url})


@users_bp.route("/oauth/connect/callback", methods=["POST"])
def universal_oauth_callback():

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"connected": False, "error": "Unauthorized"}), 401

    provider = session.get("oauth_provider")
    saved_state = session.pop("oauth_state", None)

    data = request.get_json()
    full_url = data.get("url")
    state = data.get("state")
    code = data.get("code")

    if not provider:
        return jsonify({"connected": False, "error": "Missing provider"}), 400

    if not state or state != saved_state:
        return jsonify({"connected": False, "error": "Invalid state"}), 400

    try:
        redirect_uri = f"{os.getenv('BASE_FRNT_URL')}/oauth/connect/callback"

        # ---------------- GOOGLE ----------------
        if provider == "google":
            flow = Flow.from_client_secrets_file(
                "client_secrets.json",
                scopes=g_basescopes,
                redirect_uri=redirect_uri,
            )

            flow.fetch_token(authorization_response=full_url)
            credentials = flow.credentials

            access_token = credentials.token
            refresh_token = credentials.refresh_token
            expiry = credentials.expiry
            client_id = credentials.client_id
            client_secret = credentials.client_secret

            userinfo = requests.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            ).json()

            external_user_id = userinfo.get("sub")
            email = userinfo.get("email")

        # ---------------- MICROSOFT ----------------
        elif provider == "microsoft":
            token_data = exchange_microsoft_code(code)

            if "access_token" not in token_data:
                return (
                    jsonify(
                        {
                            "connected": False,
                            "error": token_data.get(
                                "error_description", "Token exchange failed"
                            ),
                        }
                    ),
                    400,
                )

            access_token = token_data["access_token"]
            refresh_token = token_data.get("refresh_token")
            expiry = token_data.get("expires_in")
            client_id = M_CLIENT_ID  # token_data.get("client_id")
            client_secret = M_CLIENT_SECRET  # token_data.get("client_secret")

            userinfo = get_microsoft_user(access_token)

            external_user_id = userinfo["id"]
            email = userinfo.get("mail") or userinfo.get("userPrincipalName")

        else:
            return jsonify({"connected": False, "error": "Unsupported provider"}), 400

        # ---------------- SAVE INTEGRATION ----------------
        save_integration(
            primary_user_id=user_id,
            provider=provider,
            external_user_id=external_user_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            expiry=expiry,
        )

        # Audit logging
        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=OAUTH_TOKEN_STORED,
            endpoint="/oauth/connect/callback",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
            metadata={
                "provider": provider,
                "external_user_id": external_user_id,
            },
        )
        g.audit_logged = True

        return jsonify({"connected": True, "platform": provider, "email": email}), 200

    except Exception as e:
        return jsonify({"connected": False, "error": str(e)}), 500


def save_integration(
    primary_user_id,
    provider,
    external_user_id,
    email,
    access_token,
    refresh_token,
    client_id,
    client_secret,
    expiry,
):
    conn = connect_to_rds()
    cursor = conn.cursor(pymysql.cursors.DictCursor)

    query = "SELECT * FROM integrations where primary_user_id_fk = %s"

    cursor.execute(query, (primary_user_id,))
    integration = cursor.fetchone()

    if not integration:
        cursor.execute(
            """
            INSERT INTO integrations (
                integration_id,
                primary_user_id_fk,
                platform,
                user_id,
                email,
                type,
                access_token,
                refresh_token,
                client_id,
                client_secret,
                expiry,
                status,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s,%s,%s, %s,%s,%s, %s, %s, %s, 'active', NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                access_token = VALUES(access_token),
                refresh_token = VALUES(refresh_token),
                expiry = VALUES(expiry),
                status = 'active',
                updated_at = NOW()
        """,
            (
                str(uuid.uuid4()),
                primary_user_id,
                provider,
                external_user_id,
                email,
                "mails",
                access_token,
                refresh_token,
                client_id,
                client_secret,
                expiry,
            ),
        )

        cursor.execute(
            """
            INSERT INTO integrations (
                integration_id,
                primary_user_id_fk,
                platform,
                user_id,
                email,
                type,
                access_token,
                refresh_token,
                client_id,
                client_secret,
                expiry,
                status,
                created_at,
                updated_at
            )
            VALUES (%s, %s, %s,%s,%s, %s,%s,%s, %s, %s, %s, 'active', NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                access_token = VALUES(access_token),
                refresh_token = VALUES(refresh_token),
                expiry = VALUES(expiry),
                status = 'active',
                updated_at = NOW()
        """,
            (
                str(uuid.uuid4()),
                primary_user_id,
                provider,
                external_user_id,
                email,
                "drive",
                access_token,
                refresh_token,
                client_id,
                client_secret,
                expiry,
            ),
        )
        cursor.execute(
            """
            UPDATE users SET social = %s , token = %s WHERE user_id = %s""",
            (provider, access_token, primary_user_id),
        )

    conn.commit()
    conn.close()


def build_microsoft_auth_url():
    AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"

    redirect_uri = f"{os.getenv('BASE_FRNT_URL')}/oauth/connect/callback"

    state = secrets.token_urlsafe(32)

    params = {
        "client_id": M_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": " ".join(M_SCOPES),
        "state": state,
        "prompt": "select_account",
    }

    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    return auth_url, state


def exchange_microsoft_code(code):
    TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    data = {
        "client_id": M_CLIENT_ID,
        "client_secret": M_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": f"{os.getenv('BASE_FRNT_URL')}/oauth/connect/callback",
    }

    response = requests.post(TOKEN_URL, data=data)

    if response.status_code != 200:
        raise Exception(f"Token exchange failed: {response.text}")

    return response.json()


def get_microsoft_user(access_token):
    headers = {"Authorization": f"Bearer {access_token}"}

    response = requests.get("https://graph.microsoft.com/v1.0/me", headers=headers)

    if response.status_code != 200:
        raise Exception(f"User fetch failed: {response.text}")

    return response.json()


@users_bp.route("/add_domain", methods=["POST"])
def add_domain():
    data = request.get_json()
    user_id = data.get("user_id")
    email = data.get("email")
    new_domain = data.get("new_domain")
    conn = None
    try:
        if not is_valid_domain(new_domain):
            return jsonify({"error": "Invalid or non-existing domain"}), 400
        conn = connect_to_rds()
        raw_domains = fetch_user_domains(user_id, conn)
        user_domain = email.split("@")[-1].lower() if email and "@" in email else ""

        # Normalize legacy shapes: column may be missing, a plain string, or a JSON list.
        if isinstance(raw_domains, dict):
            primary = (raw_domains.get("primary") or user_domain or "").lower()
            secondary_raw = raw_domains.get("secondary") or []
            if not isinstance(secondary_raw, list):
                secondary_raw = [secondary_raw]
        elif isinstance(raw_domains, list):
            primary = user_domain
            secondary_raw = raw_domains
        elif isinstance(raw_domains, str) and raw_domains:
            primary = raw_domains.lower()
            secondary_raw = []
        else:
            primary = user_domain
            secondary_raw = []

        secondary_domains = [
            str(d).lower() for d in secondary_raw if isinstance(d, str) and d
        ]
        domains = {"primary": primary, "secondary": secondary_domains}

        # Avoid duplicates
        new_domain_lower = new_domain.lower()
        if new_domain_lower != primary and new_domain_lower not in secondary_domains:
            secondary_domains.append(new_domain_lower)

        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE users
                SET domain = %s
                WHERE user_id = %s
            """,
                (json.dumps(domains), user_id),
            )

        conn.commit()

        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=DOMAIN_ADDED,
            endpoint="/add_domain",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
            metadata={"domain": new_domain},
        )
        g.audit_logged = True

        return jsonify({"message": "Domain added successfully", "domains": domains})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# validation of domain


def is_valid_domain(domain: str):
    try:
        domain = domain.strip().lower()
        pattern = r"^(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$"

        match = re.match(pattern, domain)
        print(match, domain)

        if not match:
            return False

        # DNS Validation (MX)
        dns.resolver.resolve(domain, "MX")

        return True  # ✅ IMPORTANT

    except Exception as e:
        print("Error:", e)
        return False


@users_bp.route("/delete_domain", methods=["DELETE"])
def delete_domain():
    data = request.get_json()
    user_id = data.get("user_id")
    domain_name = data.get("domain_name")
    if not data or not user_id or not domain_name:
        return (
            jsonify({"status": False, "message": "user_id and domain_name required"}),
            400,
        )
    try:
        conn = connect_to_rds()

        domains = fetch_user_domains(user_id, conn)

        if domains["primary"].lower() == domain_name.lower():
            return (
                jsonify({"status": False, "message": "Can't delete primary domain"}),
                401,
            )

        result = delete_user_domain(user_id, domain_name, conn)

        conn.close()

        actor_email = get_email_by_id(user_id)
        log_audit_event(
            action=DOMAIN_DELETED,
            endpoint="/delete_domain",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=actor_email,
            metadata={"domain": domain_name},
        )
        g.audit_logged = True

        return jsonify({"status": True, "message": "Deleted successfully"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()


# @users_bp.route("/get_all_domain/<user_id>", methods=["GET"])
# def get_all_domain(user_id):
#     try:
#         conn = connect_to_rds()
#         domains = fetch_user_domains(user_id, conn)
#         conn.close()
#         if not domains:
#             return jsonify({"message": "No domains available"}), 200
#         all_domains = []

#         if domains.get("primary"):
#             all_domains.append(domains["primary"])

#         if domains.get("secondary"):
#             all_domains.extend(domains["secondary"])
#         # for domain in domains:
#         #     if isinstance(domain, dict):
#         #         if domain.get("primary"):
#         #             all_domains.append(domain["primary"])

#         #         if domain.get("secondary"):
#         #             all_domains.extend(domain["secondary"])

#         return jsonify({"domains":all_domains}), 200

#     except Exception as e:
#         return jsonify({"error": str(e)}), 500


@users_bp.route("/get_all_domain/<user_id>", methods=["GET"])
def get_all_domain(user_id):
    try:
        conn = connect_to_rds()
        domains = fetch_user_domains(user_id, conn)
        conn.close()

        if not domains:
            return jsonify({"message": "No domains available"}), 200

        all_domains = []

        if isinstance(domains, dict):
            if domains.get("primary"):
                all_domains.append(domains["primary"])
            if domains.get("secondary"):
                all_domains.extend(domains["secondary"])

        return jsonify({"domains": all_domains}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/get-encryption-key/<user_id>", methods=["GET"])
def get_encryption_key(user_id):
    # SECURITY PATCH: Require session auth + self-only access (or admin)
    current_user_id = session.get("user_id")
    if not current_user_id:
        return jsonify({"error": "Unauthorized"}), 401

    # Allow self-access or admin access
    if current_user_id != user_id:
        try:
            conn = connect_to_rds()
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT user_type FROM users WHERE user_id = %s", (current_user_id,)
            )
            current_user = cursor.fetchone()
            conn.close()
            if not current_user or current_user["user_type"] != "admin":
                return jsonify({"error": "Forbidden"}), 403
        except Exception:
            return jsonify({"error": "Forbidden"}), 403

    try:
        kms_service = SecureKMSService()
        plain_key, encrypted_key = kms_service.get_user_key(user_id)
        encrypted_key_b64 = base64.b64encode(encrypted_key).decode("utf-8")
        return jsonify({"encryption_key": encrypted_key_b64}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@users_bp.route("/rotate-encryption-key", methods=["POST"])
def rotate_encryption_key():
    # SECURITY PATCH: Require session auth + self-only or admin access
    current_user_id = session.get("user_id")
    if not current_user_id:
        return jsonify({"error": "Unauthorized"}), 401

    try:
        data = request.get_json()
        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "user_id is required"}), 400

        conn = connect_to_rds()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT user_type FROM users WHERE user_id = %s", (current_user_id,)
        )
        current_user = cursor.fetchone()

        # Allow self-access or admin access
        if current_user_id != user_id:
            if not current_user or current_user["user_type"] != "admin":
                conn.close()
                return jsonify({"error": "Forbidden"}), 403

        cursor.execute("SELECT user_type FROM users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        is_admin = result and result["user_type"] == "admin"
        kms_service = SecureKMSService()
        rotate = kms_service.rotate_user_key(user_id, is_admin)

        actor_email = get_email_by_id(current_user_id)
        log_audit_event(
            action=ENCRYPTION_KEY_ROTATED,
            endpoint="/rotate-encryption-key",
            ip=request.remote_addr,
            status="success",
            actor_user_id=current_user_id,
            actor_email=actor_email,
        )
        g.audit_logged = True

        return jsonify({"encryption_key": rotate["encrypted_key"]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# TEMP DEV ROUTE — remove after Phase 1 validation
@users_bp.route("/debug/set-session", methods=["POST"])
def debug_set_session():
    if os.getenv("DEV") != "true":
        return jsonify({"error": "not available"}), 404
    data = request.get_json()
    session["user_id"] = data.get("user_id")
    if data.get("workspace_id"):
        session["active_workspace_id"] = data.get("workspace_id")
    return jsonify({"ok": True}), 200
