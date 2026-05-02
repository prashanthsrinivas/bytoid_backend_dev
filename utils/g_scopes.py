g_basescopes = (
    # Identity
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    # Gmail — compose covers drafts.create/update and messages.send (superset of gmail.send)
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    # Drive — drive.file (non-restricted) + metadata.readonly for browsing
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    # Calendar
    "https://www.googleapis.com/auth/calendar",
    # Contacts
    "https://www.googleapis.com/auth/contacts.readonly",
)
