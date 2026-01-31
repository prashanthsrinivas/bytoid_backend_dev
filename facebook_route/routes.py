from flask import Blueprint, request, jsonify, session, redirect
from .meta import FacebookOAuthHandler

facebook_bp = Blueprint("facebook", __name__)


@facebook_bp.route("/auth/facebook/callback")
def facebook_callback():
    handler = FacebookOAuthHandler()
    code = request.args.get("code")
    # print("facebnook initiated",code)

    if not code:
        return "Authorization code not provided", 400

    # Exchange code for access token
    data = handler.exchange_code_for_token(code)
    # print("data",data)

    if "access_token" not in data:
        return jsonify({"error": "Failed to obtain access token", "details": data}), 400

    access_token = data["access_token"]

    # Retrieve user information
    user_info = handler.get_user_info(access_token)

    if "id" not in user_info:
        return (
            jsonify(
                {"error": "Failed to retrieve user information", "details": user_info}
            ),
            400,
        )

    # Store user info in session
    session["user"] = {
        "id": user_info["id"],
        "name": user_info.get("name"),
        "email": user_info.get("email"),
        "pages": user_info.get("accounts", {}).get("data", []),
    }

    return redirect("/radar")


@facebook_bp.route("/auth/facebook/deauthorize", methods=["POST"])
def facebook_deauthorize():
    data = request.get_json()
    user_id = data.get("user_id")

    if not user_id:
        return jsonify({"error": "User ID not provided"}), 400

    session.pop("user", None)

    return jsonify({"status": "User deauthorized", "user_id": user_id}), 200
