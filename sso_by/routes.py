from flask import Blueprint, jsonify, request, session, redirect
from db.db_checkers import check_onboarding_user, fetch_apikey_from_launch
from db.rds_db import connect_to_rds
from services.credit_system import CreditManager
from utils.app_configs import ALLOWED_ORIGINS, ACCESSIBLE_IDS
from onelogin.saml2.auth import OneLogin_Saml2_Auth

import os
import json

# Admin CONTROL 
ALLOWED_ADMINS = ["service@bytoid.ca", "beta@bytoid.ai"]

sso_bp = Blueprint("sso", __name__)

def get_or_create_user(user_id, email, name):
   conn = None
   cursor = None
   try:
       conn = connect_to_rds()
       cursor = conn.cursor()

       # check if user exists
       cursor.execute(
           "SELECT user_id FROM users WHERE user_id = %s",
           (user_id,)
       )
       existing = cursor.fetchone()

       if not existing:
           cursor.execute(
               "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s)",
               (user_id, email, name)
           )
           conn.commit()

       return {"user_id": user_id, "email": email, "name": name}

   except Exception as e:
       print("User DB error:", e)
       return None
   
   finally:
       if cursor:
           cursor.close()
       if conn:
           conn.close()
       




# =========================
# VALIDATIONS
# =========================
def is_valid_org(org, user_id=None):
   try:
       conn = connect_to_rds()
       cursor = conn.cursor()

       cursor.execute(
           "SELECT company_name FROM company WHERE company_name = %s AND deleted_at IS NULL",
           ((org or "").strip().lower(),)
       )

       result = cursor.fetchone()

       cursor.close()
       conn.close()

       return bool(result)

   except Exception as e:
       print("ORG VALIDATION ERROR:", e)
       return False



def is_domain_allowed(org, email_domain, user_id=None):
   email_domain = (email_domain or "").strip().lower()
   org = (org or "").strip().lower()

   conn = None
   cursor = None

   try:
       conn = connect_to_rds()
       cursor = conn.cursor()

       cursor.execute(
           """
           SELECT primary_domain, secondary_domains 
           FROM company 
           WHERE company_name = %s AND deleted_at IS NULL
           """,
           (org,)
       )

       row = cursor.fetchone()

       if not row:
           return False

       primary = (row[0] or "").strip().lower()
       secondary = row[1]

       # ✅ SAFE JSON PARSE
       if isinstance(secondary, str):
           try:
               secondary = json.loads(secondary)
           except Exception:
               secondary = []

       if not isinstance(secondary, list):
           secondary = []

       secondary = [str(d).lower().strip() for d in secondary]

       return email_domain == primary or email_domain in secondary

   except Exception as e:
       print("DOMAIN VALIDATION ERROR:", e)
       return False
   finally:
       if cursor:
           cursor.close()
       if conn:
           conn.close()
           


# =========================
# VALIDATE ORG (FRONTEND)
# =========================
@sso_bp.route("/org/validate", methods=["POST"])
def validate_org():
   data = request.json or {}
   org = (data.get("org") or "").strip().lower()
   user_id = data.get("user_id")

   if not org:
       return jsonify({"valid": False, "error": "Missing org"}), 400

   if not is_valid_org(org):
       return jsonify({"valid": False, "error": "INVALID_ORG"}), 400

   return jsonify({"valid": True}), 200


# =========================
# SAML HELPERS
# =========================
def prepare_flask_request(request):
   proto = request.headers.get("X-Forwarded-Proto", "https")
   host = request.headers.get("X-Forwarded-Host", request.host)

   return {
       "https": "on" if proto == "https" else "off",
       "http_host": host,
       "server_port": None,
       "script_name": request.path,
       "get_data": request.args.copy(),
       "post_data": request.form.copy(),
   }


def init_saml_auth(req):
   org = session.get("saml_org")

   if not org:
       raise Exception("Missing org in session")

   saml_path = os.path.join(os.getcwd(), "saml", "bytoid")

   return OneLogin_Saml2_Auth(req, custom_base_path=saml_path)


# =========================
# SAML LOGIN
# =========================
@sso_bp.route("/auth/saml/login", strict_slashes=False)
@sso_bp.route("/auth/saml/login/", strict_slashes=False)
def saml_login():
   session.clear()
   org = (request.args.get("org") or "").strip().lower()
   

   if not org:
       return "Missing organization/domain", 400
   

   if not is_valid_org(org):
       return jsonify({"error": "INVALID_ORG"}), 400

   origin = request.args.get("redirect")

   if origin not in ALLOWED_ORIGINS:
       origin = "https://app.bytoid.ai"

   session["saml_org"] = org
   session["saml_redirect"] = origin

   req = prepare_flask_request(request)

   try:
       auth = OneLogin_Saml2_Auth(
           req,
           custom_base_path=os.path.join(os.getcwd(), "saml", "bytoid")
       )
       return redirect(auth.login(force_authn=True))

   except Exception as e:
       print("LOGIN ERROR:", str(e))
       return f"SSO not configured for org '{org}'", 404




# =========================
# SAML ACS
# =========================
@sso_bp.route("/auth/saml/acs", methods=["POST"])
def saml_acs():
   import pymysql
   import uuid


   conn = None
   cursor = None

   try:
       req = prepare_flask_request(request)
       auth = init_saml_auth(req)

       auth.process_response()
       errors = auth.get_errors()

       if errors:
           return jsonify({"error": errors}), 400

       if not auth.is_authenticated():
           return jsonify({"error": "Not authenticated"}), 401

       user_data = auth.get_attributes()

       # ROLE EXTRACTION FROM SAML
       role = user_data.get("role", ["bytoid-user"])[0]

       if role == "bytoid-admin":
           user_role = "admin"
       else:
           user_role = "user"

       email = (
           user_data.get("email", [None])[0]
           or user_data.get(
               "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
               [None],
           )[0]
       )
       

       if not email:
           return jsonify({"error": "Email not found"}), 400

       email = email.lower()
       name = user_data.get("name", [""])[0]

       user_id = user_data.get(
           "http://schemas.microsoft.com/identity/claims/objectidentifier",
           [email],
       )[0]

       session["user_id"] = user_id
       session["auth_type"] = "saml"

       domain = email.split("@")[-1]
       org = session.get("saml_org")

       if not org:
           return jsonify({"error": "SESSION_EXPIRED"}), 401

       #  DOMAIN VALIDATION
       if not is_domain_allowed(org, domain):
           return jsonify({"error": "DOMAIN NOT ALLOWED"}), 401

       # ================= DB START =================
       conn = connect_to_rds()
       cursor = conn.cursor(pymysql.cursors.DictCursor)
       

       cursor.execute(
           "SELECT user_id, user_type FROM users WHERE user_id = %s OR email = %s",
           (user_id, email)
       )

       existing_user = cursor.fetchone()
       if user_role == "admin":
            created_by = user_id
       else:
            cursor.execute(
                """
                SELECT user_id FROM users
                WHERE company_name = %s AND user_type = 'admin'
                LIMIT 1
                """,
                (org,)
            )
            admin = cursor.fetchone()
            if not admin:
                user_role = "admin"
                created_by = user_id 
            else:
                created_by = admin["user_id"]

       if existing_user:
           cursor.execute(
               """
               UPDATE users SET 
                   first_name = %s,
                   last_name = %s,
                   social = %s,
                   user_type = %s,
                   company_name = %s,
                   logged_in_at = NOW(),
                   updated_in = NOW()
               WHERE user_id = %s 
               """,
               (name, "", "saml",user_role, org, user_id),
           )

       else:      
           cursor.execute(
               """
               INSERT INTO users (
                   user_id, user_type,
                   first_name, last_name, email,
                   social, company_name, created_by, 
                   created_in, updated_in, logged_in_at
               )
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),NOW())
               """,
               (user_id, user_role, name, "", email, "saml", org, created_by),
           )

           #  SAME AS MICROSOFT FLOW
           new_api_key = str(uuid.uuid4())
           new_launch_id = str(uuid.uuid4())
           new_sub_agent_id = str(uuid.uuid4())

           cursor.execute(
               """
               INSERT INTO subagents (
                   sub_agent_id, launch_id_fk, name
               ) VALUES (%s, %s, %s)
               """,
               (new_sub_agent_id, None, "Default Agent"),
           )

           cursor.execute(
               """
               INSERT INTO launch (
                   launch_id, sub_agent_id_fk, user_id_fk, api_id
               )
               VALUES (%s, %s, %s, %s)
               """,
               (new_launch_id, new_sub_agent_id, user_id, new_api_key),
           )

           cursor.execute(
               """
               UPDATE subagents 
               SET launch_id_fk = %s 
               WHERE sub_agent_id = %s
               """,
               (new_launch_id, new_sub_agent_id),
           )

       conn.commit()
      

       redirect_base = session.get("saml_redirect", "https://app.bytoid.ai")

       return redirect(f"{redirect_base}/sso?status=success&userid={user_id}&service=saml")

   except Exception as e:
       print("SAML ERROR:", str(e))
       return jsonify({"error": str(e)}), 500

   finally:
       if cursor:
           cursor.close()
       if conn:
           conn.close()


# =========================
# ORG CREATE
# =========================
@sso_bp.route("/org/create", methods=["POST"])
def create_org():
   data = request.json or {}

   company_name = (data.get("company_name") or "").strip().lower()
   primary = (data.get("primary_domain") or "").strip().lower()
   secondary = data.get("secondary_domains", [])

   if not company_name or not primary:
       return jsonify({"error": "Missing data"}), 400

   try:
       conn = connect_to_rds()
       cursor = conn.cursor()

       #  Check if org already exists
       cursor.execute(
           "SELECT primary_domain, secondary_domains FROM company WHERE company_name = %s AND deleted_at IS NULL",
           (company_name,)
       )
       existing = cursor.fetchone()

       secondary_clean = [d.strip().lower() for d in (secondary or []) if d]

       if existing:
           # UPDATE FLOW
           existing_primary = (existing[0] or "").strip().lower()
           existing_secondary = existing[1]

           # parse JSON safely
           if isinstance(existing_secondary, str):
               try:
                   existing_secondary = json.loads(existing_secondary)
               except:
                   existing_secondary = []

           if not isinstance(existing_secondary, list):
               existing_secondary = []

           existing_secondary = [d.strip().lower() for d in existing_secondary]

           #  merge domains (avoid duplicates)
           all_domains = set(existing_secondary)
           all_domains.update(secondary_clean)

           # if new domain is different from primary → add to secondary
           if primary != existing_primary:
               all_domains.add(primary)

           cursor.execute(
               """
               UPDATE company
               SET secondary_domains = %s
               WHERE company_name = %s
               """,
               (json.dumps(list(all_domains)), company_name)
           )

           conn.commit()

           return jsonify({"message": "Domain updated"}), 200

       else:
           # CREATE FLOW
           cursor.execute(
               """
               INSERT INTO company (company_name, primary_domain, secondary_domains)
               VALUES (%s, %s, %s)
               """,
               (
                   company_name,
                   primary,
                   json.dumps(secondary_clean)
               )
           )

           conn.commit()

           return jsonify({"message": "Company created"}), 200

   except Exception as e:
       print("CREATE/UPDATE COMPANY ERROR:", e)
       return jsonify({"error": str(e)}), 500

   finally:
       cursor.close()
       conn.close()

@sso_bp.route("/org/list", methods=["GET"])
def list_orgs():
   user_id = request.args.get("user_id")

   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute("""
       SELECT company_name, primary_domain, secondary_domains
       FROM company
       WHERE deleted_at IS NULL
   """)

   rows = cursor.fetchall()

   result = []
   for row in rows:
       secondary = json.loads(row[2]) if row[2] else []
       result.append({
           "name": row[0],
           "primary": row[1],
           "secondary": secondary
       })

   cursor.close()
   conn.close()

   return jsonify(result)





# =========================
# DASHBOARD
# =========================
@sso_bp.route("/dashboard")
def dashboard():
   if "user_id" not in session or session["user_id"] not in ACCESSIBLE_IDS:
       return redirect("/auth/saml/login")

   if "user" not in session:
       return redirect("/auth/saml/login")

   return "Dashboard"