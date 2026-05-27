"""NeMo Guardrails singleton client.

Reads config from the directory at NEMO_GUARDRAILS_CONFIG_PATH (default:
ai_governance/config/nemo_guardrails).  The LLMRails instance is created
lazily on first call to get_rails() and cached for the lifetime of the
process.  reload_rails() discards the cache so the next call re-initialises.
"""

import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nemoguardrails import LLMRails

_lock = threading.Lock()
_rails: "LLMRails | None" = None

NEMO_CONFIG_PATH = os.getenv(
    "NEMO_GUARDRAILS_CONFIG_PATH",
    "ai_governance/config/nemo_guardrails",
)


def get_rails() -> "LLMRails":
    """Return the shared LLMRails singleton, initialising it on first call."""
    global _rails
    if _rails is not None:
        return _rails
    with _lock:
        if _rails is None:
            from nemoguardrails import LLMRails, RailsConfig

            config = RailsConfig.from_path(NEMO_CONFIG_PATH)
            _rails = LLMRails(config)
    return _rails


def reload_rails() -> None:
    """Discard the cached LLMRails instance so the next get_rails() call
    re-reads config from disk.  Used by the superuser /guardrails/reload
    endpoint to hot-reload Colang flows without restarting the process."""
    global _rails
    with _lock:
        _rails = None
