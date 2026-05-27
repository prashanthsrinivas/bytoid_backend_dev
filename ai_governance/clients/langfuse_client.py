"""Langfuse observability client singleton.

Required env vars:
    LANGFUSE_PUBLIC_KEY   — project public key
    LANGFUSE_SECRET_KEY   — project secret key
    LANGFUSE_HOST         — optional, defaults to https://cloud.langfuse.com
"""

import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langfuse import Langfuse

_lock = threading.Lock()
_client: "Langfuse | None" = None


def get_langfuse() -> "Langfuse":
    """Return the shared Langfuse singleton, initialising it on first call."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            from langfuse import Langfuse

            _client = Langfuse(
                public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
                secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                host=os.getenv(
                    "LANGFUSE_HOST", "https://cloud.langfuse.com"
                ),
            )
    return _client
