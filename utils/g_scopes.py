g_basescopes = (
    # Identity
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
    # Gmail
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    # Drive — drive.file (non-restricted) + metadata.readonly for browsing
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    # Calendar
    "https://www.googleapis.com/auth/calendar",
)
