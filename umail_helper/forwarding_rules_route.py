from flask import Blueprint, request, jsonify, session
from datetime import datetime, timezone
import traceback

# Create blueprint
forwarding_bp = Blueprint("forwarding", __name__, url_prefix="/forwarding")

# Mock data (replace with database later)
forwarding_rules = {}
rule_id_counter = 1


@forwarding_bp.route("/rules", methods=["POST"])
def create_forwarding_rule():
    """
    Create a new forwarding rule

    Request body:
    {
        "keywords": ["refund", "billing", "payment"],
        "forward_to_type": "user" OR "role",
        "forward_to_id": "user_id_123" OR "role_id",
        "forward_to_display_name": "John Doe (john@company.com)" OR "Admin Role"
    }

    Response:
    {
        "status": "success",
        "rule_id": "rule_123",
        "message": "Forwarding rule created successfully"
    }
    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "User not authenticated"}), 401

        data = request.get_json()

        # Validate required fields
        keywords = data.get("keywords", [])
        forward_to_type = data.get("forward_to_type")  # "user" or "role"
        forward_to_id = data.get("forward_to_id")
        forward_to_display_name = data.get("forward_to_display_name")

        if not keywords or len(keywords) == 0:
            return jsonify({"error": "At least one keyword is required"}), 400

        if forward_to_type not in ["user", "role"]:
            return jsonify({"error": "forward_to_type must be 'user' or 'role'"}), 400

        if not forward_to_id:
            return jsonify({"error": "forward_to_id is required"}), 400

        if not forward_to_display_name:
            return jsonify({"error": "forward_to_display_name is required"}), 400

        # Create rule
        global rule_id_counter
        rule_id = f"rule_{rule_id_counter}"
        rule_id_counter += 1

        forwarding_rules[rule_id] = {
            "id": rule_id,
            "keywords": [k.strip().lower() for k in keywords],  # Normalize keywords
            "forward_to_type": forward_to_type,
            "forward_to_id": forward_to_id,
            "forward_to_display_name": forward_to_display_name,
            "created_by": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "is_active": True,
        }

        # print(f"✅ Forwarding rule created: {rule_id}")
        # print(f"   Keywords: {keywords}")
        # print(f"   Forward to ({forward_to_type}): {forward_to_display_name}")

        return (
            jsonify(
                {
                    "status": "success",
                    "rule_id": rule_id,
                    "message": "Forwarding rule created successfully",
                }
            ),
            201,
        )

    except Exception as e:
        # print(f"❌ Error creating forwarding rule: {e}")
        # print(f"📋 Traceback: {traceback.format_exc()}")
        return (
            jsonify(
                {
                    "status": "error",
                    "error": str(e),
                    "message": "Failed to create forwarding rule",
                }
            ),
            500,
        )


@forwarding_bp.route("/rules", methods=["GET"])
def get_forwarding_rules():
    """
    Get all forwarding rules for current user

    Response:
    {
        "status": "success",
        "rules": [
            {
                "id": "rule_1",
                "keywords": ["refund", "billing"],
                "forward_to_type": "user",
                "forward_to_id": "user_123",
                "forward_to_display_name": "John Doe",
                "is_active": true,
                "created_at": "2025-01-15T10:30:00Z"
            },
            {
                "id": "rule_2",
                "keywords": ["urgent", "critical"],
                "forward_to_type": "role",
                "forward_to_id": "admin",
                "forward_to_display_name": "Admin Role",
                "is_active": true,
                "created_at": "2025-01-14T15:20:00Z"
            }
        ]
    }
    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "User not authenticated"}), 401

        rules_list = list(forwarding_rules.values())

        # print(f"✅ Retrieved {len(rules_list)} forwarding rules")

        return jsonify({"status": "success", "rules": rules_list}), 200

    except Exception as e:
        # print(f"❌ Error retrieving forwarding rules: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@forwarding_bp.route("/rules/<rule_id>", methods=["PUT"])
def update_forwarding_rule(rule_id):
    """
    Update a forwarding rule (toggle active status or edit)

    Request body:
    {
        "is_active": true/false
    }

    Response:
    {
        "status": "success",
        "message": "Rule updated successfully"
    }
    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "User not authenticated"}), 401

        if rule_id not in forwarding_rules:
            return jsonify({"error": "Rule not found"}), 404

        data = request.get_json()

        if "is_active" in data:
            forwarding_rules[rule_id]["is_active"] = data["is_active"]
            status = "Active" if data["is_active"] else "Inactive"
            # print(f"✅ Rule {rule_id} status changed to: {status}")

        return (
            jsonify({"status": "success", "message": "Rule updated successfully"}),
            200,
        )

    except Exception as e:
        # print(f"❌ Error updating rule: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@forwarding_bp.route("/rules/<rule_id>", methods=["DELETE"])
def delete_forwarding_rule(rule_id):
    """
    Delete a forwarding rule

    Response:
    {
        "status": "success",
        "message": "Rule deleted successfully"
    }
    """
    try:
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "User not authenticated"}), 401

        if rule_id not in forwarding_rules:
            return jsonify({"error": "Rule not found"}), 404

        del forwarding_rules[rule_id]

        # print(f"✅ Rule {rule_id} deleted")

        return (
            jsonify({"status": "success", "message": "Rule deleted successfully"}),
            200,
        )

    except Exception as e:
        # print(f"❌ Error deleting rule: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500


@forwarding_bp.route("/check/<email_subject>", methods=["GET"])
def check_email_forwarding(email_subject):
    """
    Check if an email should be forwarded based on keywords
    Called when new email arrives

    Returns list of forwarding destinations

    Response:
    {
        "status": "success",
        "should_forward": true,
        "forward_to": [
            {
                "type": "user",
                "id": "user_123",
                "display_name": "John Doe"
            },
            {
                "type": "role",
                "id": "admin",
                "display_name": "Admin Role"
            }
        ]
    }
    """
    try:
        email_subject_lower = email_subject.lower()
        forward_to = []

        # Check all active rules
        for rule in forwarding_rules.values():
            if not rule["is_active"]:
                continue

            # Check if any keyword matches email subject
            for keyword in rule["keywords"]:
                if keyword in email_subject_lower:
                    forward_to.append(
                        {
                            "type": rule["forward_to_type"],
                            "id": rule["forward_to_id"],
                            "display_name": rule["forward_to_display_name"],
                        }
                    )
                    break  # Don't add same rule twice

        should_forward = len(forward_to) > 0

        # if should_forward:
        #     #print(
        #         f"✅ Email '{email_subject}' matches {len(forward_to)} forwarding rule(s)"
        #     )

        return (
            jsonify(
                {
                    "status": "success",
                    "should_forward": should_forward,
                    "forward_to": forward_to,
                }
            ),
            200,
        )

    except Exception as e:
        # print(f"❌ Error checking email forwarding: {e}")
        return jsonify({"status": "error", "error": str(e)}), 500
