import json
import pymysql
from datetime import datetime
from utils.s3_utils import read_json_from_s3, s3bucket, S3_BUCKET
from db.rds_db import connect_to_rds
from utils.base_logger import get_logger

logger = get_logger(__name__)

COLOR_PALETTE = ["black", "blue", "green", "yellow", "pink", "orange"]

PERMISSION_MAP = {
    "radar": "kb.doc.view",
    "runbook": "compliance.runbook.read",
}


def save_json_to_s3(data, s3_key):
    """Write JSON data to S3."""
    try:
        s3 = s3bucket()
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json.dumps(data, default=str),
            ContentType="application/json",
        )
        logger.info(f"Saved to S3: {s3_key}")
        return True
    except Exception as e:
        logger.error(f"Failed to save JSON to S3: {e}", exc_info=True)
        return False


def get_admin_shared_config(admin_id):
    """Read admin's shared configuration from S3."""
    s3_key = f"{admin_id}/runbook/sharedconfigs.json"
    config = read_json_from_s3(s3_key)
    if not config:
        return {"users": {}, "reports": {}}
    return config


def save_admin_shared_config(admin_id, config):
    """Save admin's shared configuration to S3."""
    s3_key = f"{admin_id}/runbook/sharedconfigs.json"
    return save_json_to_s3(config, s3_key)


def get_user_shared_reports(user_id):
    """Read user's shared reports index from S3."""
    s3_key = f"{user_id}/reports/shared_reports.json"
    reports = read_json_from_s3(s3_key)
    if not reports:
        return {}
    return reports


def save_user_shared_reports(user_id, reports):
    """Save user's shared reports index to S3."""
    s3_key = f"{user_id}/reports/shared_reports.json"
    return save_json_to_s3(reports, s3_key)


def get_next_color(existing_entries):
    """Find the next available color from the palette."""
    used_colors = {entry.get("colorindication") for entry in existing_entries}
    for color in COLOR_PALETTE:
        if color not in used_colors:
            return color
    return COLOR_PALETTE[-1]


def get_role_users_from_db(conn, admin_id, role_id):
    """Get all users in the admin's organization who have the given role."""
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                """
                SELECT user_id, email, permissions
                FROM users
                WHERE (company_name = (SELECT company_name FROM users WHERE user_id=%s)
                       OR launch_id_fk = (SELECT launch_id_fk FROM users WHERE user_id=%s))
                AND user_type = 'user'
                AND JSON_UNQUOTE(JSON_EXTRACT(permissions, '$.role.id')) = %s
                AND JSON_UNQUOTE(JSON_EXTRACT(permissions, '$.status')) = 'active'
                """,
                (admin_id, admin_id, role_id),
            )
            rows = cursor.fetchall()
            return rows or []
    except Exception as e:
        logger.error(f"Error fetching role users: {e}", exc_info=True)
        return []


def check_user_has_permission(user_permissions_json, required_permission):
    """Check if user's permissions JSON includes the required permission."""
    try:
        if isinstance(user_permissions_json, str):
            perms = json.loads(user_permissions_json)
        else:
            perms = user_permissions_json

        role = perms.get("role", {})
        if not role or perms.get("status") != "active":
            return False
        return required_permission in role.get("permissions", [])
    except Exception as e:
        logger.error(f"Error checking user permission: {e}", exc_info=True)
        return False


def check_role_has_permission(conn, admin_id, role_id, required_permission):
    """Check if a role in the admin's roles_creation has the required permission."""
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT roles_creation FROM users WHERE user_id=%s",
                (admin_id,),
            )
            row = cursor.fetchone()
            if not row or not row["roles_creation"]:
                return False

            roles = json.loads(row["roles_creation"])
            role = next((r for r in roles if r.get("id") == role_id), None)
            if not role:
                return False
            return required_permission in role.get("permissions", [])
    except Exception as e:
        logger.error(f"Error checking role permission: {e}", exc_info=True)
        return False


def get_round_robin_user(admin_id, role_id, report_type, conn, required_permission):
    """Get the user in the role with the fewest reports of the given type."""
    role_users = get_role_users_from_db(conn, admin_id, role_id)

    if not role_users:
        return None, "No users found in this role"

    eligible_users = []
    for user in role_users:
        if check_user_has_permission(user.get("permissions"), required_permission):
            eligible_users.append(user)

    if not eligible_users:
        return None, f"No users in this role have permission: {required_permission}"

    config = get_admin_shared_config(admin_id)
    user_counts = {}

    for user_id, user_data in config.get("users", {}).items():
        report_type_key = f"{report_type}_count"
        user_counts[user_id] = user_data.get(report_type_key, 0)

    chosen_user = min(
        eligible_users,
        key=lambda u: user_counts.get(u["user_id"], 0)
    )

    return chosen_user, None


def core_assign_report(
    admin_id,
    admin_email,
    user_id,
    user_email,
    report_id,
    report_type,
    report_name,
    conn,
    dbserver,
    parent_id=None,
):
    """
    Core logic to assign a report to a user.
    Updates sharedconfigs.json, user's shared_reports.json, and LanceDB document_meta.
    """
    try:
        required_permission = PERMISSION_MAP.get(report_type, "kb.doc.view")

        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT permissions FROM users WHERE user_id=%s",
                (user_id,),
            )
            user_row = cursor.fetchone()
            if not user_row:
                return None, "Target user not found"

            if not check_user_has_permission(user_row.get("permissions"), required_permission):
                return None, f"User does not have required permission: {required_permission}"

        config = get_admin_shared_config(admin_id)

        if "reports" not in config:
            config["reports"] = {}
        if report_id not in config["reports"]:
            config["reports"][report_id] = {"sharing_access": []}
        if parent_id and not config["reports"][report_id].get("runbook_id"):
            config["reports"][report_id]["runbook_id"] = parent_id

        sharing_access = config["reports"][report_id].get("sharing_access", [])

        admin_entry = next((e for e in sharing_access if e["id"] == admin_id), None)
        if not admin_entry:
            sharing_access.append(
                {
                    "id": admin_id,
                    "email": admin_email,
                    "colorindication": "black",
                    "access": True,
                }
            )

        for entry in sharing_access:
            if entry["id"] != admin_id and entry.get("access"):
                entry["access"] = False

        user_entry = next((e for e in sharing_access if e["id"] == user_id), None)
        if not user_entry:
            next_color = get_next_color(sharing_access)
            sharing_access.append(
                {
                    "id": user_id,
                    "email": user_email,
                    "colorindication": next_color,
                    "access": True,
                }
            )
        else:
            user_entry["access"] = True

        config["reports"][report_id]["sharing_access"] = sharing_access

        if "users" not in config:
            config["users"] = {}
        if user_id not in config["users"]:
            config["users"][user_id] = {"email": user_email, "runbook_count": 0, "radar_count": 0, "reports": []}

        user_data = config["users"][user_id]
        report_type_key = f"{report_type}_count"

        report_entry = next((r for r in user_data.get("reports", []) if r["id"] == report_id), None)
        if not report_entry:
            new_entry = {
                "type": report_type,
                "id": report_id,
                "name": report_name,
                "assigned_at": datetime.utcnow().isoformat(),
            }
            if parent_id:
                new_entry["runbook_id"] = parent_id
            user_data["reports"].append(new_entry)
            user_data[report_type_key] = user_data.get(report_type_key, 0) + 1

        save_admin_shared_config(admin_id, config)

        user_shared_reports = get_user_shared_reports(user_id)
        entry = {
            "reportid": report_id,
            "dateofaccess": datetime.utcnow().isoformat(),
            "mainuser_id": admin_id,
            "name": report_name,
            "type": report_type,
        }
        if parent_id:
            entry["runbook_id"] = parent_id
        user_shared_reports[report_id] = entry
        save_user_shared_reports(user_id, user_shared_reports)

        import asyncio

        async def update_lancedb_meta():
            try:
                if report_type == "radar":
                    record = await dbserver.radar_get_by_id(admin_id, report_id)
                    if record:
                        result = record.get("result")
                        if isinstance(result, str):
                            result = json.loads(result)
                        if "document_meta" not in result:
                            result["document_meta"] = {}
                        result["document_meta"]["sharing_access"] = sharing_access
                        await dbserver.radar_update_result(
                            admin_id,
                            record.get("review_id"),
                            result,
                        )
                elif report_type == "runbook":
                    result_record = await dbserver.runbook_get_result(admin_id, report_id)
                    if result_record and result_record.get("status") != "not_found":
                        result_doc = result_record.get("result")
                        if isinstance(result_doc, str):
                            result_doc = json.loads(result_doc)
                        if "document_meta" not in result_doc:
                            result_doc["document_meta"] = {}
                        result_doc["document_meta"]["sharing_access"] = sharing_access
                        await dbserver.update_runbook_result(
                            admin_id,
                            report_id,
                            result_doc,
                        )
            except Exception as e:
                logger.error(f"Failed to update LanceDB meta: {e}", exc_info=True)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(update_lancedb_meta())
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"Could not update LanceDB meta: {e}")

        return sharing_access, None

    except Exception as e:
        logger.error(f"Error in core_assign_report: {e}", exc_info=True)
        return None, str(e)


def core_revoke_report(admin_id, user_id, report_id, report_type, dbserver):
    """
    Core logic to revoke access to a report from a user.
    Updates sharedconfigs.json, user's shared_reports.json, and LanceDB document_meta.
    """
    try:
        config = get_admin_shared_config(admin_id)

        if report_id in config.get("reports", {}):
            sharing_access = config["reports"][report_id].get("sharing_access", [])
            for entry in sharing_access:
                if entry["id"] == user_id:
                    entry["access"] = False

            config["reports"][report_id]["sharing_access"] = sharing_access

        if user_id in config.get("users", {}):
            user_data = config["users"][user_id]
            user_data["reports"] = [r for r in user_data.get("reports", []) if r["id"] != report_id]
            report_type_key = f"{report_type}_count"
            user_data[report_type_key] = max(0, user_data.get(report_type_key, 0) - 1)

        save_admin_shared_config(admin_id, config)

        user_shared_reports = get_user_shared_reports(user_id)
        if report_id in user_shared_reports:
            del user_shared_reports[report_id]
        save_user_shared_reports(user_id, user_shared_reports)

        import asyncio

        sharing_access = config.get("reports", {}).get(report_id, {}).get("sharing_access", [])

        async def update_lancedb_meta_revoke():
            try:
                if report_type == "radar":
                    record = await dbserver.radar_get_by_id(admin_id, report_id)
                    if record:
                        result = record.get("result")
                        if isinstance(result, str):
                            result = json.loads(result)
                        if "document_meta" not in result:
                            result["document_meta"] = {}
                        result["document_meta"]["sharing_access"] = sharing_access
                        await dbserver.radar_update_result(
                            admin_id,
                            record.get("review_id"),
                            result,
                        )
                elif report_type == "runbook":
                    result_record = await dbserver.runbook_get_result(admin_id, report_id)
                    if result_record and result_record.get("status") != "not_found":
                        result_doc = result_record.get("result")
                        if isinstance(result_doc, str):
                            result_doc = json.loads(result_doc)
                        if "document_meta" not in result_doc:
                            result_doc["document_meta"] = {}
                        result_doc["document_meta"]["sharing_access"] = sharing_access
                        await dbserver.update_runbook_result(
                            admin_id,
                            report_id,
                            result_doc,
                        )
            except Exception as e:
                logger.error(f"Failed to update LanceDB meta on revoke: {e}", exc_info=True)

        try:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(update_lancedb_meta_revoke())
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"Could not update LanceDB meta on revoke: {e}")

        return sharing_access, None

    except Exception as e:
        logger.error(f"Error in core_revoke_report: {e}", exc_info=True)
        return None, str(e)
