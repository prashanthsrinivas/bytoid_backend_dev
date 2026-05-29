from flask import Blueprint, jsonify, request, session, redirect, g
from db.db_checkers import (
    check_onboarding_user,
    fetch_apikey_from_launch,
    get_email_by_id,
    is_invited_user,
)
from db.rds_db import connect_to_rds
from services.credit_system import CreditManager
from services.audit_log_service import (
    log_audit_event,
    SPECIAL_ACCESS_GRANTED,
    SPECIAL_ACCESS_REVOKED,
    SAML_USER_PROVISIONED,
    ORG_CREATED,
    SSO_RBAC_APPLIED,
)
from utils.app_configs import ALLOWED_ORIGINS, ACCESSIBLE_IDS
from db.db_checkers import ensure_starter_credits_for_user
from utils.base_logger import get_logger
from onelogin.saml2.auth import OneLogin_Saml2_Auth

import os
import json
import requests
from datetime import datetime
from urllib.parse import urlsplit

logger = get_logger(__name__)

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
        cursor.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
        existing = cursor.fetchone()

        if not existing:
            cursor.execute(
                "INSERT INTO users (user_id, email, name) VALUES (%s, %s, %s)",
                (user_id, email, name),
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
            ((org or "").strip().lower(),),
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
            (org,),
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
        (user_id, org),
    )

    user = cursor.fetchone()

    cursor.close()
    conn.close()

    return user and user[0] == "admin"


@sso_bp.route("/check-microsoft", methods=["GET"])
def check_microsoft():
    user_id = request.args.get("user_id")
    if not user_id:
        return jsonify({"connected": False})
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute("SELECT token FROM users WHERE user_id = %s", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return jsonify({"connected": bool(row and row[0])})


TENANT_ID = os.getenv("AZURE_TENANT_ID")
CLIENT_ID = os.getenv("AZURE_CLIENT_ID")
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET")
REDIRECT_URI = os.getenv("AZURE_REDIRECT_URI")


@sso_bp.route("/microsoft/disconnect", methods=["POST"])
def disconnect_microsoft():
    data = request.get_json()
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    try:
        conn = connect_to_rds()
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET token = NULL WHERE user_id = %s", (user_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({"message": "Outlook disconnected successfully"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@sso_bp.route("/microsoft/login")
def microsoft_login():
    user_id = request.args.get("user_id")

    auth_url = (
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/authorize?"
        f"client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        f"&response_mode=query"
        f"&scope=offline_access Mail.Send User.Read"
        f"&state={user_id}"
    )

    return redirect(auth_url)


@sso_bp.route("/microsoft/callback")
def microsoft_callback():
    code = request.args.get("code")
    user_id = request.args.get("state")

    if not code or not user_id:
        return redirect(f"{os.getenv('BASE_FRNT_URL')}/login")

    token_url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
        "scope": "offline_access Mail.Send User.Read",
    }
    res = requests.post(token_url, data=data)
    token_data = res.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return jsonify({"error": "Token not received"}), 400
    conn = connect_to_rds()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET token=%s WHERE user_id=%s", (access_token, user_id)
    )
    print("SESSION USER_ID:", session.get("user_id"))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(
        f"{os.getenv('BASE_FRNT_URL')}/sso?status=success&userid={user_id}&service=microsoft"
    )


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


def _resolve_return_origin():
    """Pick a safe frontend origin to return to after the SAML round-trip.

    Resolution order, each validated against ALLOWED_ORIGINS:
      1. Explicit ``?redirect=`` query param (the FE's stated return target).
      2. The request's ``Origin`` header.
      3. The origin parsed from the ``Referer`` header.
      4. Last resort: the production app URL.

    This keeps a local/preview frontend on its own domain through the SAML
    flow instead of hard-bouncing every login to production. Only origins
    already on the allowlist are honored, so it can't be used to redirect to
    an attacker-controlled site.
    """
    for raw in (
        request.args.get("redirect"),
        request.headers.get("Origin"),
        request.headers.get("Referer"),
    ):
        if not raw:
            continue
        parts = urlsplit(raw)
        # Candidate forms to match: the raw value, the scheme://host origin, and
        # the bare host (the allowlist carries some entries without a scheme).
        forms = [raw]
        clean_origin = None
        if parts.scheme and parts.netloc:
            clean_origin = f"{parts.scheme}://{parts.netloc}"
            forms.extend([clean_origin, parts.netloc])
        for form in forms:
            if form in ALLOWED_ORIGINS:
                # Return a clean scheme://host (strips any Referer path/query).
                return clean_origin or form

    return "https://app.bytoid.ai"


# =========================
# SAML LOGIN
# =========================
@sso_bp.route("/auth/saml/login", strict_slashes=False)
@sso_bp.route("/auth/saml/login/", strict_slashes=False)
def saml_login():
    session.pop("saml_org", None)
    session.pop("saml_redirect", None)
    org = (request.args.get("org") or "").strip().lower()

    if not org:
        return "Missing organization/domain", 400

    if not is_valid_org(org):
        return jsonify({"error": "INVALID_ORG"}), 400

    # Resolve where to send the user after SAML completes. Honors the explicit
    # ?redirect= param, then the Origin/Referer of this request (all validated
    # against ALLOWED_ORIGINS), instead of hard-defaulting every login to prod.
    origin = _resolve_return_origin()

    session["saml_org"] = org
    session["saml_redirect"] = origin

    req = prepare_flask_request(request)

    try:
        auth = OneLogin_Saml2_Auth(
            req, custom_base_path=os.path.join(os.getcwd(), "saml", "bytoid")
        )
        return redirect(auth.login(force_authn=True))

    except Exception as e:
        print("LOGIN ERROR:", str(e))
        return f"SSO not configured for org '{org}'", 404


def _apply_pending_sso_rbac(cursor, conn, user_id, email, org):
    """Apply pending SSO RBAC invite on login. Idempotent."""
    # Skip if user already has an active role
    cursor.execute("SELECT permissions FROM users WHERE user_id=%s", (user_id,))
    my_row = cursor.fetchone()
    existing_perms = {}
    if my_row and my_row.get("permissions"):
        try:
            existing_perms = json.loads(my_row["permissions"])
        except Exception:
            pass
    if existing_perms.get("status") == "active" and existing_perms.get("role"):
        return

    # Find pending SSO invite across all org admins
    cursor.execute(
        "SELECT user_id, email, permissions FROM users "
        "WHERE company_name=%s AND user_type='admin' AND social='saml'",
        (org,),
    )
    admins = cursor.fetchall()

    matched_admin = matched_entry = None
    for admin in admins:
        if not admin.get("permissions"):
            continue
        try:
            admin_perms = json.loads(admin["permissions"])
        except Exception:
            continue
        for entry in admin_perms.get("invites", []):
            if (
                entry.get("invite_type") == "sso"
                and entry.get("email", "").lower() == email.lower()
                and entry.get("status") == "pending"
            ):
                matched_admin = admin
                matched_entry = entry
                break
        if matched_admin:
            break

    if not matched_admin or not matched_entry:
        return

    # Write role to user's permissions
    role_payload = {
        "role": matched_entry["role"],
        "status": "active",
        "email": email,
        "invited_by": matched_entry.get("invited_by"),
        "created_at": matched_entry.get("created_at"),
        "applied_at": datetime.utcnow().isoformat(),
    }
    cursor.execute(
        "UPDATE users SET permissions=%s WHERE user_id=%s",
        (json.dumps(role_payload), user_id),
    )

    # Move entry from admin's invites → shared
    try:
        admin_perms = json.loads(matched_admin["permissions"])
    except Exception:
        admin_perms = {}
    admin_perms.setdefault("invites", [])
    admin_perms.setdefault("shared", [])
    admin_perms["invites"] = [
        e
        for e in admin_perms["invites"]
        if not (
            e.get("invite_type") == "sso"
            and e.get("email", "").lower() == email.lower()
        )
    ]
    admin_perms["shared"].append(
        {
            "email": email,
            "role": matched_entry["role"],
            "invited_by": matched_entry.get("invited_by"),
            "status": "active",
            "invite_type": "sso",
            "accepted_at": datetime.utcnow().isoformat(),
        }
    )
    cursor.execute(
        "UPDATE users SET permissions=%s WHERE user_id=%s",
        (json.dumps(admin_perms), matched_admin["user_id"]),
    )
    conn.commit()

    # Log audit event
    try:
        log_audit_event(
            action=SSO_RBAC_APPLIED,
            endpoint="/auth/saml/acs",
            ip=request.remote_addr if request else "unknown",
            status="success",
            actor_user_id=matched_admin["user_id"],
            actor_email=matched_admin.get("email", "unknown"),
            target_email=email,
            metadata={
                "role_id": matched_entry["role"].get("id"),
                "role_name": matched_entry["role"].get("name"),
            },
        )
    except Exception as log_exc:
        logger.error(f"[_apply_pending_sso_rbac] Audit log failed: {log_exc}")


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
            or user_data.get(
                "http://schemas.microsoft.com/ws/2008/06/identity/claims/role"
            )
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
            or user_data.get(
                "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
                [None],
            )[0]
        )

        if not email or "@" not in email:
            return jsonify({"error": "Invalid email"}), 400

        email = email.lower()
        name = user_data.get("name", [""])[0]

        user_id = str(
            user_data.get(
                "http://schemas.microsoft.com/identity/claims/objectidentifier",
                [email],
            )[0]
        ).strip()

        session["user_id"] = user_id
        session.pop("active_workspace_id", None)
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
        is_new_user = not bool(existing_user)

        if existing_user:
            # ✅ Only update if this is INVITED USER
            if existing_user["user_id"] == email:
                cursor.execute(
                    """
                    UPDATE users
                    SET user_id = %s,
                        company_name = %s,
                        social = 'saml',
                        has_access = 1  
                    WHERE email = %s
                """,
                    (user_id, org, email),
                )
                conn.commit()

                # activate invited RBAC role
                cursor.execute(
                    """
                    SELECT permissions
                    FROM users
                    WHERE email = %s
                """,
                    (email,),
                )

                perm_row = cursor.fetchone()

                if perm_row and perm_row["permissions"]:

                    permissions_data = json.loads(perm_row["permissions"])

                    permissions_data["status"] = "active"

                    cursor.execute(
                        """
                        UPDATE users
                        SET permissions = %s
                        WHERE user_id = %s
                    """,
                        (json.dumps(permissions_data), user_id),
                    )

                    conn.commit()

            # ✅ If already real user → DO NOT overwrite
            else:
                user_id = existing_user["user_id"]
            user_role = existing_user["user_type"]
        else:
            # normal new user flow (keep your existing code)
            user_type = "admin" if role == "bytoid-admin" else "user"
            cursor.execute(
                """
                INSERT INTO users
                (user_id, email, user_type, company_name, created_by, has_access)
                VALUES (%s, %s, %s, %s, %s, 1)
                """,
                (user_id, email, user_type, org, user_id),
            )
            conn.commit()
            user_role = user_type

        # ================= ROLE VALIDATION + SYNC =================

        ROLE_MAP = {"bytoid-admin": "admin", "bytoid-user": "user"}

        # 1. STRICT VALIDATION (rule satisfied)
        if role not in ROLE_MAP:
            return jsonify({"error": "Invalid role from IDP"}), 403

        # 2. SAFE MAPPING
        new_role = ROLE_MAP[role]

        # 3. SYNC DB IF DIFFERENT (no blind overwrite)
        if user_role != new_role:
            cursor.execute(
                """
                UPDATE users
                SET user_type = %s
                WHERE user_id = %s
                """,
                (new_role, user_id),
            )
            conn.commit()

        user_role = new_role

        # Apply RBAC role if pending SSO invite exists (only for bytoid-user)
        if user_role == "user":
            _apply_pending_sso_rbac(cursor, conn, user_id, email, org)

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
        # ================= CREDITS LOGIC =================
        try:
            # Ensure starter credits exist (IMPORTANT)
            ensure_starter_credits_for_user(user_id, conn)
            credits = CreditManager(conn)
            avail_credits = credits.check_if_remaining(user_id=user_id)
            credit_status = avail_credits.get("status")
            credit_message = avail_credits.get("message")
        except Exception as e:
            print("CREDIT ERROR:", str(e))
            credit_status = "error"
            credit_message = "Could not fetch credits"

        # Audit logging
        log_audit_event(
            action=SAML_USER_PROVISIONED,
            endpoint="/auth/saml/acs",
            ip=request.remote_addr,
            status="success",
            actor_user_id=user_id,
            actor_email=email,
            metadata={
                "saml_org": org,
                "is_new_user": is_new_user,
                "role": role,
            },
        )
        g.audit_logged = True

        redirect_base = session.get("saml_redirect", "https://app.bytoid.ai")
        invited_user = is_invited_user(user_id, conn)

        return redirect(
            f"{redirect_base}/sso?status=success&userid={user_id}&service=saml"
            f"&credits={credit_status}&invited_user={invited_user}"
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
        (requesting_user, org),
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
        (target_user, org),
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
            (company_name,),
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
                (json.dumps(list(all_domains)), company_name),
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
                (company_name, primary, json.dumps(secondary_clean)),
            )

            conn.commit()

            # Audit logging (only for create, not update)
            actor_user_id = getattr(g, "session_user_id", None)
            actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
            log_audit_event(
                action=ORG_CREATED,
                endpoint="/org/create",
                ip=request.remote_addr,
                status="success",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                metadata={
                    "org_name": company_name,
                    "primary_domain": primary,
                    "secondary_domains": secondary_clean,
                },
            )
            g.audit_logged = True

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
        result.append({"name": row[0], "primary": row[1], "secondary": secondary})

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
        (org,),
    )

    users = cursor.fetchall()

    #  Apply access filter BEFORE closing connection
    filtered_users = [u for u in users if can_access_user(user_id, u[0], org)]

    cursor.close()
    conn.close()

    return jsonify(filtered_users)


def has_access(user_id, org):
    conn = connect_to_rds()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT user_type, has_access FROM users WHERE user_id = %s AND company_name = %s",
        (user_id, org),
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
        (user_id, org),
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
        (admin_id, admin_id, target_user, org),
    )

    conn.commit()
    cursor.close()
    conn.close()

    actor_email = get_email_by_id(admin_id)
    target_email = get_email_by_id(target_user)
    log_audit_event(
        action=SPECIAL_ACCESS_GRANTED,
        endpoint="/admin/grant-access",
        ip=request.remote_addr,
        status="success",
        actor_user_id=admin_id,
        actor_email=actor_email,
        target_user_id=target_user,
        target_email=target_email,
        metadata={"org": org},
    )
    g.audit_logged = True

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
        (admin_id, target_user, org),
    )

    conn.commit()
    cursor.close()
    conn.close()

    actor_email = get_email_by_id(admin_id)
    target_email = get_email_by_id(target_user)
    log_audit_event(
        action=SPECIAL_ACCESS_REVOKED,
        endpoint="/admin/revoke-access",
        ip=request.remote_addr,
        status="success",
        actor_user_id=admin_id,
        actor_email=actor_email,
        target_user_id=target_user,
        target_email=target_email,
        metadata={"org": org},
    )
    g.audit_logged = True

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
