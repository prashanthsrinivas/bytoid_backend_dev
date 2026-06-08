"""Free/open-source OSINT collectors (run inside the Lambda).

Each collector is deterministic and keyless — no paid APIs, no LLM (LLM-based
summarization is deferred to the app-side AI risk analysis where it is
credit-gated). Every outbound call goes through ``vra.osint.safe_fetch``.
"""

from vra.osint.collectors.base import (  # noqa: F401
    BaseCollector,
    CollectorContext,
    default_collectors,
    run_collection,
)
