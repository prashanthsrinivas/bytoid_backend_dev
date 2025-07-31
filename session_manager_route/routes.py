from flask import Blueprint, request, jsonify,make_response,session
from .session_manager import SessionManager

session_bp = Blueprint('session', __name__)

session_manager = SessionManager()
EXEMPT_PATHS = ['generate_session','get_google_client_id', 'google_login','login','oauth2callback']
@session_bp.before_request
def validate_cookie_session():
    if request.endpoint in EXEMPT_PATHS or request.endpoint is None:
        return
    session_id = request.cookies.get('session_id')
    print("session id",session_id)
    if session_id:
        if not session_manager.is_session_valid(session_id):
            # Invalidate expired session
            session_manager.delete_session(session_id)
            response = make_response({'error': 'Session expired'}, 440)  
            response.delete_cookie('session_id')
            return response
        else:
            session_manager.update_session_timestamp(session_id)
            
@session_bp.route("/session_exists", methods=["POST"])
def session_exists():
    print("Received request:",request.cookies)
    session_id = request.cookies.get('session_id')
    print("Session id:", session_id)

    if not session_id:
        return jsonify({'error': 'session_id is required'}), 400

    if session_manager.is_session_valid(session_id):
        return jsonify({'session_exists': True}), 200
    else:
        return jsonify({'session_exists': False}), 200


@session_bp.route('/delete_session', methods=['POST'])
def delete_session():
    session_id = request.cookies.get('session_id')

    if not session_id:
        return jsonify({'error': 'Missing session_id cookie'}), 400

    # Attempt to delete the session
    deleted = session_manager.delete_session(session_id)

    # Optionally clear the cookie from the client as well
    response = make_response(jsonify({'deleted': deleted}))
    response.set_cookie('session_id', '', expires=0)

    return response

# @session_bp.route("/generate_session", methods=["POST"])
@session_bp.route("/generate_session")
def generate_session():
   
    try:
        session_id = session_manager.create_session()

        response = make_response(jsonify({"message": "Session created"}))
        response.set_cookie(
            "session_id",
            value=session_id,
            httponly=True,     # Prevents access via JavaScript
            secure=True,       # Only send over HTTPS
            samesite="None",    # Or "Strict"/"None" depending on your setup
            path="/",
            max_age = 10800
        )
        return response
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@session_bp.route('/debug_session')
def debug_session():
    return jsonify(dict(session))