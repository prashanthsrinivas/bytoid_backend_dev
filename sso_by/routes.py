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

       # SAFE JSON PARSE
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

def is_admin(user_id, org):
   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       """
       SELECT user_type FROM users 
       WHERE user_id = %s AND company_name = %s
       """,
       (user_id, org)
   )

   user = cursor.fetchone()

   cursor.close()
   conn.close()

   return user and user[0] == "admin"
           


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

       # ================= ROLE =================
       roles = (
           user_data.get("role")
           or user_data.get("http://schemas.microsoft.com/ws/2008/06/identity/claims/role")
           or []
       )

       if not isinstance(roles, list) or len(roles) == 0:
           return jsonify({"error": "Role not received from IDP"}), 403

       role = roles[0]

       if role not in ["bytoid-admin", "bytoid-user"]:
           return jsonify({"error": "Invalid role from IDP"}), 403

       # ================= EMAIL =================
       email = (
           user_data.get("email", [None])[0]
           or user_data.get("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress", [None])[0]
       )

       if not email or "@" not in email:
           return jsonify({"error": "Invalid email"}), 400

       email = email.lower()
       name = user_data.get("name", [""])[0]

       user_id = str(user_data.get(
           "http://schemas.microsoft.com/identity/claims/objectidentifier",
           [email],
       )[0]).strip()

       session["user_id"] = user_id
       session["auth_type"] = "saml"

       domain = email.split("@")[-1]
       org = session.get("saml_org")

       if not org:
           return jsonify({"error": "SESSION_EXPIRED"}), 401

       # ================= DOMAIN CHECK =================
       if not is_domain_allowed(org, domain):
           return jsonify({"error": "DOMAIN NOT ALLOWED"}), 401

       # ================= DB =================
       conn = connect_to_rds()
       cursor = conn.cursor(pymysql.cursors.DictCursor)

       cursor.execute(
           """
           SELECT user_id, user_type, company_name 
           FROM users 
           WHERE (user_id = %s OR email = %s) 
           AND (company_name = %s OR company_name IS NULL)
           """,
           (user_id, email, org),
       )

       existing_user = cursor.fetchone()

       # ================= AUTO ONBOARDING =================
       if existing_user:
           if not existing_user["company_name"]:
               cursor.execute(
                   """
                   UPDATE users 
                   SET company_name = %s
                   WHERE user_id = %s
                   """,
                   (org, user_id)
                )
               conn.commit()
           user_role = existing_user["user_type"]
       else:
           user_type = "admin" if role == "bytoid-admin" else "user"
           cursor.execute(
               """
               INSERT INTO users 
               (user_id, email, user_type, company_name, created_by, has_access)
               VALUES (%s, %s, %s, %s, %s, 1)
               """,
               (user_id, email, user_type, org, user_id)
            )
           conn.commit()
           user_role = user_type
    
       # ================= ROLE VALIDATION =================

       if role == "bytoid-admin" and user_role != "admin":
           return jsonify({"error": "Role mismatch"}), 403

       if role == "bytoid-user" and user_role != "user":
           return jsonify({"error": "Role mismatch"}), 403

       # ================= SESSION =================
       session["user_role"] = user_role

       # ================= UPDATE USER =================
       cursor.execute(
           """
           UPDATE users SET 
               first_name = %s,
               last_name = %s,
               social = %s,
               company_name = %s,
               logged_in_at = NOW(),
               updated_in = NOW()
           WHERE user_id = %s
           """,
           (name, "", "saml", org, user_id),
       )

       conn.commit()

       redirect_base = session.get("saml_redirect", "https://app.bytoid.ai")

       return redirect(
           f"{redirect_base}/sso?status=success&userid={user_id}&service=saml"
       )

   except Exception as e:
       print("SAML ERROR:", str(e))
       return jsonify({"error": str(e)}), 500

   finally:
       if cursor:
           cursor.close()
       if conn:
           conn.close()

def can_access_user(requesting_user, target_user, org):
   conn = connect_to_rds()
   cursor = conn.cursor()

   #  Get requesting user role
   cursor.execute(
       "SELECT user_type FROM users WHERE user_id = %s AND company_name = %s",
       (requesting_user, org)
   )
   req = cursor.fetchone()
   if not req:
       cursor.close()
       conn.close()
       return False

   requesting_role = req[0]

   #  Get target user details
   cursor.execute(
       """
       SELECT user_id, user_type, shared_with, created_by
       FROM users
       WHERE user_id = %s AND company_name = %s
       """,
       (target_user, org)
   )
   row = cursor.fetchone()

   if not row:
       cursor.close()
       conn.close()
       return False

   owner_id, target_role, shared_with, created_by = row

   #  1. Own record
   if requesting_user == owner_id:
       cursor.close()
       conn.close()
       return True

   #  2. Creator access
   if requesting_user == created_by:
       cursor.close()
       conn.close()
       return True

   # 3. ADMIN RULE (KEY LOGIC)
   if requesting_role == "admin":
       if target_role == "user":
           #  Admin sees ALL users in org by default
           cursor.close()
           conn.close()
           return True

       if target_role == "admin":
           #  Admin cannot see other admins unless shared
           if shared_with:
               try:
                   shared = json.loads(shared_with)
                   if requesting_user in shared:
                       cursor.close()
                       conn.close()
                       return True
               except:
                   pass

           cursor.close()
           conn.close()
           return False

   #  4. Shared access (for non-admin users)
   if shared_with:
       try:
           shared = json.loads(shared_with)
           if requesting_user in shared:
               cursor.close()
               conn.close()
               return True
       except:
           pass

   cursor.close()
   conn.close()
   return False
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

@sso_bp.route("/admin/users", methods=["GET"])
def get_all_users():
   user_id = session.get("user_id")
   org = session.get("saml_org")

   if not user_id or not org or not is_admin(user_id, org):
       return jsonify({"error": "Unauthorized"}), 403

   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       """
       SELECT user_id, email, user_type, has_access 
       FROM users 
       WHERE company_name = %s
       """,
       (org,)
   )

   users = cursor.fetchall()

   #  Apply access filter BEFORE closing connection
   filtered_users = [
       u for u in users
       if can_access_user(user_id, u[0], org)
   ]

   cursor.close()
   conn.close()

   return jsonify(filtered_users)

def has_access(user_id, org):
   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       "SELECT user_type, has_access FROM users WHERE user_id = %s AND company_name = %s",
       (user_id, org)
   )

   result = cursor.fetchone()

   cursor.close()
   conn.close()

   if not result:
       return False

   user_type, access = result
   if user_type == "admin":
       return True

   return bool(access)

def has_admin_access(user_id, org):
   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       "SELECT user_type, has_access FROM users WHERE user_id = %s AND company_name = %s",
       (user_id, org)
   )

   result = cursor.fetchone()

   cursor.close()
   conn.close()

   if not result:
       return False

   user_type, access = result

   return user_type == "admin" and bool(access)

@sso_bp.route("/admin/grant-access", methods=["POST"])
def grant_access():
   data = request.json
   target_user = data.get("user_id")

   if not target_user:
       return jsonify({"error": "Missing user_id"}), 400

   admin_id = session.get("user_id")
   org = session.get("saml_org")

   if not admin_id or not org or not is_admin(admin_id, org):
       return jsonify({"error": "Unauthorized"}), 403

   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       """
       UPDATE users 
       SET shared_with = 
            CASE 
                WHEN JSON_CONTAINS(COALESCE(shared_with, JSON_ARRAY()), JSON_QUOTE(%s))
                THEN shared_with
                ELSE JSON_ARRAY_APPEND(COALESCE(shared_with, JSON_ARRAY()), '$', %s)
            END
        WHERE user_id = %s AND company_name = %s
        """,
        (admin_id, admin_id, target_user, org)
    )

   conn.commit()
   cursor.close()
   conn.close()

   return jsonify({"message": "Access granted"})
@sso_bp.route("/admin/revoke-access", methods=["POST"])
def revoke_access():
   data = request.json
   target_user = data.get("user_id")

   if not target_user:
       return jsonify({"error": "Missing user_id"}), 400

   admin_id = session.get("user_id")
   org = session.get("saml_org")

   if not admin_id or not org or not is_admin(admin_id, org):
       return jsonify({"error": "Unauthorized"}), 403

   conn = connect_to_rds()
   cursor = conn.cursor()

   cursor.execute(
       """
       UPDATE users 
       SET shared_with = JSON_REMOVE(
           shared_with,
           JSON_UNQUOTE(JSON_SEARCH(shared_with, 'one', %s))
        )
        WHERE user_id = %s AND company_name = %s
        """,
        (admin_id, target_user, org)
        )

   conn.commit()
   cursor.close()
   conn.close()

   return jsonify({"message": "Access revoked"})

# =========================
# DASHBOARD
# =========================
@sso_bp.route("/dashboard")
def dashboard():
  user_id = session.get("user_id")
  org = session.get("saml_org")

  if not user_id:
      return redirect("/auth/saml/login")

  if not has_access(user_id, org):
      return jsonify({"error": "Access denied"}), 403

  return "Dashboard"