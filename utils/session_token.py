"""
Header-based session tokens for cross-site clients.

Auth here is a Flask **signed-cookie** session (stateless: all session data lives
inside the signed cookie value). That works fine same-origin, but the app
(app.bytoid.ai / demo.bytoid.ai) talks to a separate API origin, and Safari's
ITP refuses to send that third-party cookie — so cookie auth silently dies after
login and the user gets logged out / sees no data.

Fix without changing the session model: expose the exact same signed value as a
bearer **token** (`current_session_token`) that the client can store and send as
`Authorization: Bearer <token>`. A small WSGI shim (BearerSessionMiddleware in
app.py) turns that header back into the session cookie before Flask reads it, so
every existing `session.get(...)` keeps working untouched.
"""
from flask import current_app, session


def current_session_token() -> str:
    """Serialize the current session exactly as Flask would sign the cookie.

    Returns "" when there's no secret key / serializer (so callers can no-op).
    """
    try:
        serializer = current_app.session_interface.get_signing_serializer(current_app)
        if serializer is None:
            return ""
        return serializer.dumps(dict(session))
    except Exception:
        return ""
