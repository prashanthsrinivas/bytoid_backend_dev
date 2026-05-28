"""Langfuse observability client singleton.

Required env vars to enable:
    LANGFUSE_PUBLIC_KEY   — project public key
    LANGFUSE_SECRET_KEY   — project secret key
    LANGFUSE_HOST         — optional; defaults to https://cloud.langfuse.com,
                            or point at your self-hosted Langfuse instance.

When the keys are missing `get_langfuse()` returns None instead of raising.
Routes treat that as "not configured" and respond with an empty trace list
plus a flag the frontend can use to show a hint.
"""

import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langfuse import Langfuse

_lock = threading.Lock()
_client: "Langfuse | None" = None


def is_configured() -> bool:
    """True iff the env vars needed to talk to Langfuse are present."""
    return bool(os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY"))


def get_langfuse() -> "Langfuse | None":
    """Return the shared Langfuse singleton, or None when keys are missing.

    Initialises on first call.  If the SDK itself throws during init (eg.
    bad host, network), the error is swallowed and None is returned so the
    rest of the Governance UI keeps working.
    """
    global _client
    if _client is not None:
        return _client
    if not is_configured():
        return None
    with _lock:
        if _client is None:
            try:
                from langfuse import Langfuse

                _client = Langfuse(
                    public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                    secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                    host=os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com"),
                )
            except Exception:
                _client = None
    return _client
