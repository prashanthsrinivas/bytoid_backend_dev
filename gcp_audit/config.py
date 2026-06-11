"""GCP audit configuration (env-driven). In-process collection — no Lambda."""

from __future__ import annotations

import os


def _env_true(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


GCP_AUDIT_RETENTION_DAYS = int(os.getenv("GCP_AUDIT_RETENTION_DAYS", "90"))
GCP_AUDIT_LLM_BUDGET_CHARS = int(os.getenv("GCP_AUDIT_LLM_BUDGET_CHARS", "60000"))


def auto_remediate_enabled() -> bool:
    """Gated, off by default. A real cloud write also requires an approved
    remediation workflow and an explicit non-dry-run request."""
    return _env_true("GCP_AUDIT_AUTO_REMEDIATE")
