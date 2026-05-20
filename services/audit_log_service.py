"""
Audit logging service — backend only, never expose via any route.

Writes structured JSON audit entries to S3 at {user_id}/audit/{date}.json.
"""

import json

# import logging
# import os
from datetime import datetime, timezone

# from logging.handlers import RotatingFileHandler
from flask import g, session, request
from db.db_checkers import get_email_by_id
from utils.normal import parse_composite_user_id
from utils.s3_utils import save_app_runbase_S3

# Action constants
# AUTH
LOGIN_SUCCESS = "LOGIN_SUCCESS"
LOGIN_FAILED = "LOGIN_FAILED"
USER_LOGGED_OUT = "USER_LOGGED_OUT"
PASSWORD_CHANGED = "PASSWORD_CHANGED"
PASSWORD_RESET = "PASSWORD_RESET"
TOTP_SETUP = "TOTP_SETUP"
TOTP_VERIFIED = "TOTP_VERIFIED"
EMAIL_VERIFIED = "EMAIL_VERIFIED"

# SECURITY
ENCRYPTION_KEY_ROTATED = "ENCRYPTION_KEY_ROTATED"
OAUTH_INTEGRATION_CONNECTED = "OAUTH_INTEGRATION_CONNECTED"
DOMAIN_ADDED = "DOMAIN_ADDED"
DOMAIN_DELETED = "DOMAIN_DELETED"
USER_TYPE_CHANGED = "USER_TYPE_CHANGED"

# ADMIN_ACCESS
ROLE_CREATED = "ROLE_CREATED"
ROLE_UPDATED = "ROLE_UPDATED"
ROLE_DELETED = "ROLE_DELETED"
SPECIAL_ACCESS_GRANTED = "SPECIAL_ACCESS_GRANTED"
SPECIAL_ACCESS_REVOKED = "SPECIAL_ACCESS_REVOKED"
SPECIAL_ACCESS_REQUESTED = "SPECIAL_ACCESS_REQUESTED"
SPECIAL_ACCESS_APPROVED = "SPECIAL_ACCESS_APPROVED"
SPECIAL_ACCESS_REJECTED = "SPECIAL_ACCESS_REJECTED"
WORKSPACE_ACCESS_ENTERED = "WORKSPACE_ACCESS_ENTERED"

# USER_MANAGEMENT
USER_CREATED = "USER_CREATED"
USER_INVITED = "USER_INVITED"
INVITE_CANCELLED = "INVITE_CANCELLED"
INVITE_RESENT = "INVITE_RESENT"
USER_INVITE_ACCEPTED = "USER_INVITE_ACCEPTED"
USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
USER_ACCESS_REVOKED = "USER_ACCESS_REVOKED"
USER_ACCESS_ACTIVATED = "USER_ACCESS_ACTIVATED"
USER_DELETED = "USER_DELETED"

# WORKFLOW
RUNBOOK_CREATED = "RUNBOOK_CREATED"
RUNBOOK_UPDATED = "RUNBOOK_UPDATED"
RUNBOOK_DELETED = "RUNBOOK_DELETED"
RUNBOOK_BULK_DELETED = "RUNBOOK_BULK_DELETED"
RUNBOOK_SCHEDULED = "RUNBOOK_SCHEDULED"
PLAYBOOK_CREATED = "PLAYBOOK_CREATED"
PLAYBOOK_DELETED = "PLAYBOOK_DELETED"
PLAYBOOK_CLONED = "PLAYBOOK_CLONED"
PLAYBOOK_MADE_GLOBAL = "PLAYBOOK_MADE_GLOBAL"

# EVIDENCE
EVIDENCE_CONFIG_ADDED = "EVIDENCE_CONFIG_ADDED"
EVIDENCE_CONFIG_UPDATED = "EVIDENCE_CONFIG_UPDATED"
EVIDENCE_CONFIG_DELETED = "EVIDENCE_CONFIG_DELETED"

# BILLING
CREDIT_ADDED_MANUALLY = "CREDIT_ADDED_MANUALLY"

# CONTACTS
CONTACT_CREATED = "CONTACT_CREATED"
CONTACT_UPDATED = "CONTACT_UPDATED"
CONTACT_DELETED = "CONTACT_DELETED"
CONTACT_BULK_DELETED = "CONTACT_BULK_DELETED"
CONTACT_GROUP_CREATED = "CONTACT_GROUP_CREATED"
CONTACT_GROUP_UPDATED = "CONTACT_GROUP_UPDATED"
CONTACT_GROUP_DELETED = "CONTACT_GROUP_DELETED"

# TRACKER (new)
TRACKER_CREATED = "TRACKER_CREATED"
TRACKER_DELETED = "TRACKER_DELETED"
TRACKER_MODIFIED = "TRACKER_MODIFIED"
TRACKER_ENTRY_ADDED = "TRACKER_ENTRY_ADDED"
TRACKER_COLUMN_ADDED = "TRACKER_COLUMN_ADDED"
TRACKER_COLUMN_DELETED = "TRACKER_COLUMN_DELETED"
TRACKER_EVIDENCE_UPLOADED = "TRACKER_EVIDENCE_UPLOADED"
TRACKER_FRAMEWORK_ADDED = "TRACKER_FRAMEWORK_ADDED"
TRACKER_FRAMEWORK_UPDATED = "TRACKER_FRAMEWORK_UPDATED"
TRACKER_FRAMEWORK_REMOVED = "TRACKER_FRAMEWORK_REMOVED"
TRACKER_SHARED = "TRACKER_SHARED"
TRACKER_SHARE_REVOKED = "TRACKER_SHARE_REVOKED"
TRACKER_POLICY_ADDED = "TRACKER_POLICY_ADDED"
TRACKER_POLICY_UPDATED = "TRACKER_POLICY_UPDATED"
TRACKER_POLICY_REMOVED = "TRACKER_POLICY_REMOVED"
TRACKER_POLICY_REMAPPED = "TRACKER_POLICY_REMAPPED"

# POLICY HUB share
POLICY_SHARED = "POLICY_SHARED"
POLICY_SHARE_REVOKED = "POLICY_SHARE_REVOKED"

# TRUST CENTER internal share
TRUST_CENTER_INTERNAL_SHARED = "TRUST_CENTER_INTERNAL_SHARED"
TRUST_CENTER_INTERNAL_SHARE_REVOKED = "TRUST_CENTER_INTERNAL_SHARE_REVOKED"

# REPORT share (radar + runbook)
REPORT_SHARED = "REPORT_SHARED"
REPORT_SHARE_REVOKED = "REPORT_SHARE_REVOKED"

# NOTES (new)
NOTE_CREATED = "NOTE_CREATED"
NOTE_UPDATED = "NOTE_UPDATED"
NOTE_DELETED = "NOTE_DELETED"
NOTE_SHARED = "NOTE_SHARED"

# INTEGRATIONS (new)
INTEGRATION_CONNECTED = "INTEGRATION_CONNECTED"
INTEGRATION_DISCONNECTED = "INTEGRATION_DISCONNECTED"
INTEGRATION_DELETED = "INTEGRATION_DELETED"

# PLAYBOOK extensions
PLAYBOOK_UPDATED = "PLAYBOOK_UPDATED"
PLAYBOOK_STEP_ADDED = "PLAYBOOK_STEP_ADDED"
PLAYBOOK_STEP_UPDATED = "PLAYBOOK_STEP_UPDATED"
PLAYBOOK_STEP_DELETED = "PLAYBOOK_STEP_DELETED"
PLAYBOOK_SCHEDULED = "PLAYBOOK_SCHEDULED"
PLAYBOOK_INSTALLED = "PLAYBOOK_INSTALLED"
PLAYBOOK_SHARED = "PLAYBOOK_SHARED"

# RUNBOOK extensions
RUNBOOK_RESULT_DELETED = "RUNBOOK_RESULT_DELETED"
RUNBOOK_EVIDENCE_UPDATED = "RUNBOOK_EVIDENCE_UPDATED"
RUNBOOK_EVIDENCE_ADMISSIBILITY_CHANGED = "RUNBOOK_EVIDENCE_ADMISSIBILITY_CHANGED"

# ACCOUNT (new)
PROFILE_UPDATED = "PROFILE_UPDATED"
ONBOARDING_COMPLETED = "ONBOARDING_COMPLETED"
ORG_CREATED = "ORG_CREATED"
SAML_USER_PROVISIONED = "SAML_USER_PROVISIONED"
SSO_RBAC_APPLIED = "SSO_RBAC_APPLIED"
AWS_SAML_CONNECTED = "AWS_SAML_CONNECTED"
AWS_SAML_DISCONNECTED = "AWS_SAML_DISCONNECTED"
AZURE_SAML_CONNECTED = "AZURE_SAML_CONNECTED"
AZURE_SAML_DISCONNECTED = "AZURE_SAML_DISCONNECTED"

# SECURITY extensions
API_KEY_CREATED = "API_KEY_CREATED"
OAUTH_TOKEN_STORED = "OAUTH_TOKEN_STORED"
MICROSOFT_DISCONNECTED = "MICROSOFT_DISCONNECTED"

# AI_REPORTING (new)
REPORT_CREATED = "REPORT_CREATED"
REPORT_FINALIZED = "REPORT_FINALIZED"
SPECIAL_ACCESS_MODIFIED = "SPECIAL_ACCESS_MODIFIED"
TRACKER_SHARE_REVOKED = "TRACKER_SHARE_REVOKED"
TRACKER_SHARED = "TRACKER_SHARED"
TRACKER_ROW_OPTION_REMOVED = "TRACKER_ROW_OPTION_REMOVED"
TRACKER_ROW_OPTION_ADDED = "TRACKER_ROW_OPTION_ADDED"
# Category mapping for structured queries
ACTION_CATEGORY = {
    # AUTH
    "LOGIN_SUCCESS": "auth",
    "LOGIN_FAILED": "auth",
    "USER_LOGGED_OUT": "auth",
    "PASSWORD_CHANGED": "auth",
    "PASSWORD_RESET": "auth",
    "TOTP_SETUP": "auth",
    "TOTP_VERIFIED": "auth",
    "EMAIL_VERIFIED": "auth",
    # SECURITY
    "ENCRYPTION_KEY_ROTATED": "security",
    "OAUTH_INTEGRATION_CONNECTED": "security",
    "DOMAIN_ADDED": "security",
    "DOMAIN_DELETED": "security",
    "USER_TYPE_CHANGED": "security",
    # ADMIN_ACCESS
    "SPECIAL_ACCESS_GRANTED": "admin_access",
    "SPECIAL_ACCESS_REVOKED": "admin_access",
    "SPECIAL_ACCESS_REQUESTED": "admin_access",
    "SPECIAL_ACCESS_APPROVED": "admin_access",
    "SPECIAL_ACCESS_REJECTED": "admin_access",
    "SPECIAL_ACCESS_MODIFIED": "admin_access",
    "WORKSPACE_ACCESS_ENTERED": "admin_access",
    "ROLE_CREATED": "admin_access",
    "ROLE_UPDATED": "admin_access",
    "ROLE_DELETED": "admin_access",
    # USER_MANAGEMENT
    "USER_CREATED": "user_management",
    "USER_INVITED": "user_management",
    "INVITE_CANCELLED": "user_management",
    "INVITE_RESENT": "user_management",
    "USER_INVITE_ACCEPTED": "user_management",
    "USER_ROLE_CHANGED": "user_management",
    "USER_ACCESS_REVOKED": "user_management",
    "USER_ACCESS_ACTIVATED": "user_management",
    "USER_DELETED": "user_management",
    # WORKFLOW
    "RUNBOOK_CREATED": "workflow",
    "RUNBOOK_UPDATED": "workflow",
    "RUNBOOK_DELETED": "workflow",
    "RUNBOOK_BULK_DELETED": "workflow",
    "RUNBOOK_SCHEDULED": "workflow",
    "PLAYBOOK_CREATED": "workflow",
    "PLAYBOOK_DELETED": "workflow",
    "PLAYBOOK_CLONED": "workflow",
    "PLAYBOOK_MADE_GLOBAL": "workflow",
    # EVIDENCE
    "EVIDENCE_CONFIG_ADDED": "evidence",
    "EVIDENCE_CONFIG_UPDATED": "evidence",
    "EVIDENCE_CONFIG_DELETED": "evidence",
    # BILLING
    "CREDIT_ADDED_MANUALLY": "billing",
    # CONTACTS
    "CONTACT_CREATED": "contacts",
    "CONTACT_UPDATED": "contacts",
    "CONTACT_DELETED": "contacts",
    "CONTACT_BULK_DELETED": "contacts",
    "CONTACT_GROUP_CREATED": "contacts",
    "CONTACT_GROUP_UPDATED": "contacts",
    "CONTACT_GROUP_DELETED": "contacts",
    # TRACKER
    "TRACKER_CREATED": "tracker",
    "TRACKER_DELETED": "tracker",
    "TRACKER_MODIFIED": "tracker",
    "TRACKER_ENTRY_ADDED": "tracker",
    "TRACKER_COLUMN_ADDED": "tracker",
    "TRACKER_COLUMN_DELETED": "tracker",
    "TRACKER_EVIDENCE_UPLOADED": "tracker",
    "TRACKER_SHARED": "tracker",
    "TRACKER_SHARE_REVOKED": "tracker",
    # POLICY HUB
    "POLICY_SHARED": "policy_hub",
    "POLICY_SHARE_REVOKED": "policy_hub",
    # TRUST CENTER
    "TRUST_CENTER_INTERNAL_SHARED": "trust_center",
    "TRUST_CENTER_INTERNAL_SHARE_REVOKED": "trust_center",
    # AI_REPORTING share
    "REPORT_SHARED": "ai_reporting",
    "REPORT_SHARE_REVOKED": "ai_reporting",
    # NOTES
    "NOTE_CREATED": "notes",
    "NOTE_UPDATED": "notes",
    "NOTE_DELETED": "notes",
    "NOTE_SHARED": "notes",
    # INTEGRATIONS
    "INTEGRATION_CONNECTED": "integrations",
    "INTEGRATION_DISCONNECTED": "integrations",
    "INTEGRATION_DELETED": "integrations",
    # PLAYBOOK
    "PLAYBOOK_UPDATED": "workflow",
    "PLAYBOOK_STEP_ADDED": "workflow",
    "PLAYBOOK_STEP_UPDATED": "workflow",
    "PLAYBOOK_STEP_DELETED": "workflow",
    "PLAYBOOK_SCHEDULED": "workflow",
    "PLAYBOOK_INSTALLED": "workflow",
    "PLAYBOOK_SHARED": "workflow",
    # RUNBOOK
    "RUNBOOK_RESULT_DELETED": "workflow",
    "RUNBOOK_EVIDENCE_UPDATED": "evidence",
    "RUNBOOK_EVIDENCE_ADMISSIBILITY_CHANGED": "evidence",
    # ACCOUNT
    "PROFILE_UPDATED": "account",
    "ONBOARDING_COMPLETED": "account",
    "ORG_CREATED": "account",
    "SAML_USER_PROVISIONED": "account",
    "SSO_RBAC_APPLIED": "admin_access",
    "AWS_SAML_CONNECTED": "account",
    "AWS_SAML_DISCONNECTED": "account",
    "AZURE_SAML_CONNECTED": "account",
    "AZURE_SAML_DISCONNECTED": "account",
    # SECURITY
    "API_KEY_CREATED": "security",
    "OAUTH_TOKEN_STORED": "security",
    "MICROSOFT_DISCONNECTED": "security",
    # AI_REPORTING
    "REPORT_CREATED": "ai_reporting",
    "REPORT_FINALIZED": "ai_reporting",
    "SPECIAL_ACCESS_MODIFIED": "admin_access",
}

# _AUDIT_LOG_FILE = "logs/audit.log"
# os.makedirs(os.path.dirname(_AUDIT_LOG_FILE), exist_ok=True)
#
# _audit_logger = logging.getLogger("audit")
# _audit_logger.setLevel(logging.INFO)
# _audit_logger.propagate = False
#
# if not _audit_logger.handlers:
#     _handler = RotatingFileHandler(
#         _AUDIT_LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5
#     )
#     _handler.setFormatter(logging.Formatter("%(message)s"))
#     _audit_logger.addHandler(_handler)


def _upload_to_s3(entry: dict) -> None:
    try:
        audit_owner_id = entry.get("audit_owner_id")
        actor_user_id = entry.get("actor_user_id")
        # entry["timestamp"] is UTC ISO 8601 — first 10 chars = YYYY-MM-DD
        date = entry.get("timestamp", "")[:10]

        if audit_owner_id and date:
            save_app_runbase_S3(entry, f"{audit_owner_id}/audit/{date}.json")

        # Only write actor's copy when actor differs from owner (admin delegation case)
        if actor_user_id and actor_user_id != audit_owner_id and date:
            save_app_runbase_S3(entry, f"{actor_user_id}/audit/{date}.json")
    except Exception:
        pass


def log_audit_event(
    action,
    endpoint,
    ip,
    status,
    actor_user_id=None,
    actor_email=None,
    target_user_id=None,
    target_email=None,
    acting_on_behalf_of_user_id=None,
    acting_on_behalf_of_email=None,
    metadata=None,
):
    """Write one structured JSON audit entry. Never raises."""
    try:
        # audit_owner_id = the workspace this entry belongs to (routing key for audit UI).
        # Delegation: acting_on_behalf_of_user_id = workspace owner (e.g. Test) → entry lives on Test's audit page.
        # Self-access: acting_on_behalf_of_user_id is None → audit_owner_id = actor_user_id → entry lives on actor's audit page.
        audit_owner_id = acting_on_behalf_of_user_id or actor_user_id

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "category": ACTION_CATEGORY.get(action, "api_activity"),
            "audit_owner_id": audit_owner_id,
            "actor_user_id": actor_user_id,
            "actor_email": actor_email,
            "target_user_id": target_user_id,
            "target_email": target_email,
            "acting_on_behalf_of_user_id": acting_on_behalf_of_user_id,
            "acting_on_behalf_of_email": acting_on_behalf_of_email,
            "endpoint": endpoint,
            "ip": ip,
            "status": status,
            "metadata": metadata or {},
        }
        # _audit_logger.info(json.dumps(entry, default=str))
        _upload_to_s3(entry)
    except Exception:
        pass


# def build_audit_actor(body_user_id):
#     """
#     Returns (actor_user_id, actor_email, acting_on_behalf_of_user_id, acting_on_behalf_of_email).

#     Primary signal: session["active_workspace_id"] (set by /admin/access-workspace).
#     If active, the session user is acting on behalf of the workspace owner.
#     If not active, this is self-access.
#     """
#     logged_in_user_id, user_id = parse_composite_user_id(body_user_id)
#     try:
#         session_uid = getattr(g, "session_user_id", None) or session.get("user_id")
#     except RuntimeError:
#         session_uid = None

#     # Fast path: trust delegation context already computed by audit_before_request().
#     # Avoids re-reading session which can miss when body_user_id == session_uid.
#     pre_computed_behalf = getattr(g, "acting_on_behalf_of_user_id", None)
#     if pre_computed_behalf:
#         actor_user_id = session_uid or body_user_id
#         actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
#         acting_on_behalf_of_email = getattr(g, "acting_on_behalf_of_email", None)
#         return (
#             actor_user_id,
#             actor_email,
#             pre_computed_behalf,
#             acting_on_behalf_of_email,
#         )

#     # Check if there's an active workspace delegation
#     active_workspace_id = session.get("active_workspace_id")

#     # Determine workspace owner from the strongest available signal.
#     # Primary: session["active_workspace_id"] (set by /admin/access-workspace).
#     # Fallback: body_user_id differs from session user (frontend sends workspace owner's ID).
#     workspace_owner = None
#     if active_workspace_id and session_uid and session_uid != active_workspace_id:
#         workspace_owner = active_workspace_id
#     elif session_uid and body_user_id and session_uid != body_user_id:
#         workspace_owner = body_user_id

#     if workspace_owner:
#         # Delegated: session user is operating inside another user's workspace.
#         actor_user_id = session_uid
#         actor_email = get_email_by_id(session_uid)
#         acting_on_behalf_of_user_id = workspace_owner
#         acting_on_behalf_of_email = get_email_by_id(workspace_owner)

#         # Stamp g so middleware fallback can see delegation context
#         try:
#             g.acting_on_behalf_of_user_id = workspace_owner
#             g.acting_on_behalf_of_email = acting_on_behalf_of_email
#         except RuntimeError:
#             pass
#     else:
#         # Self-access: session user is the workspace owner.
#         actor_user_id = session_uid or body_user_id
#         actor_email = get_email_by_id(actor_user_id) if actor_user_id else None
#         acting_on_behalf_of_user_id = None
#         acting_on_behalf_of_email = None

#     return (
#         actor_user_id,
#         actor_email,
#         acting_on_behalf_of_user_id,
#         acting_on_behalf_of_email,
#     )


def build_audit_actor(body_user_id):
    """
    Returns:
    (
        actor_user_id,
        actor_email,
        acting_on_behalf_of_user_id,
        acting_on_behalf_of_email,
    )

    Supports:
    - Normal user_id
        user123

    - Super-user delegation
        admin123##SU##target456
    """

    # -----------------------------------------
    # Parse composite user ID
    # -----------------------------------------
    logged_in_user_id, user_id = parse_composite_user_id(body_user_id)

    try:
        session_uid = getattr(g, "session_user_id", None) or session.get("user_id")
    except RuntimeError:
        session_uid = None

    # Prefer parsed logged-in user ID
    actor_user_id = logged_in_user_id or session_uid or user_id

    actor_email = get_email_by_id(actor_user_id) if actor_user_id else None

    # -----------------------------------------
    # Self-access
    # -----------------------------------------
    if not logged_in_user_id or logged_in_user_id == user_id:
        return (
            actor_user_id,
            actor_email,
            None,
            None,
        )

    # -----------------------------------------
    # Acting on behalf
    # -----------------------------------------
    acting_on_behalf_of_user_id = ""
    acting_on_behalf_of_email = ""
    if logged_in_user_id != user_id:
        acting_on_behalf_of_user_id = user_id

        acting_on_behalf_of_email = (
            get_email_by_id(acting_on_behalf_of_user_id)
            if acting_on_behalf_of_user_id
            else None
        )

        # Stamp request context
        try:
            g.acting_on_behalf_of_user_id = acting_on_behalf_of_user_id
            g.acting_on_behalf_of_email = acting_on_behalf_of_email
        except RuntimeError:
            pass

    return (
        actor_user_id,
        actor_email,
        acting_on_behalf_of_user_id,
        acting_on_behalf_of_email,
    )
