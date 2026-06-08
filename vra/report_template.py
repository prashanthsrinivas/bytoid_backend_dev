"""VRA report structure = the base runbook template + an OSINT section.

We do NOT fork ``runbook/default_temp.json`` (that would drift as the base
evolves). Instead we read it at runtime and append the ``osint-intelligence-
assessment`` block defined in ``osint_block.json``, returning the merged
structure to use as the runbook ``structure_theme`` for VRA assessments. The
base file is only read, never modified.

The OSINT section is inserted just before the trailing appendices/references
blocks so it reads as a first-class analysis section.
"""

from __future__ import annotations

import json
import os

from utils.base_logger import get_logger

logger = get_logger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
_BASE_TEMPLATE = os.path.join(_HERE, os.pardir, "runbook", "default_temp.json")
_OSINT_BLOCK = os.path.join(_HERE, "osint_block.json")

# Blocks the OSINT section should sit before (kept at the end of the report).
_TRAILING_BLOCK_IDS = {"appendices", "references"}

# Embedded dashboard-link placeholder the engine/frontend resolves to the live
# Vendor Intelligence Dashboard URL for the assessment.
DASHBOARD_LINK_PLACEHOLDER = "{{VRA_DASHBOARD_URL}}"


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def build_vra_structure() -> dict:
    """Return the base report structure with the OSINT block inserted.

    Idempotent and side-effect free; safe to call per assessment. Falls back to
    a minimal structure containing just the OSINT block if the base template
    can't be read, so a VRA report always has its OSINT section.
    """
    osint_block = _load(_OSINT_BLOCK)
    try:
        base = _load(_BASE_TEMPLATE)
    except Exception:
        logger.warning("default_temp.json unreadable; using OSINT-only structure", exc_info=True)
        return {"blocks": [osint_block]}

    blocks = list(base.get("blocks") or [])

    # Don't double-insert if already present.
    if any(b.get("block_id") == osint_block["block_id"] for b in blocks):
        return base

    insert_at = next(
        (i for i, b in enumerate(blocks) if b.get("block_id") in _TRAILING_BLOCK_IDS),
        len(blocks),
    )
    blocks.insert(insert_at, osint_block)
    base["blocks"] = blocks
    return base


def resolve_dashboard_url(structure: dict, assessment_id: str) -> dict:
    """Return a copy of ``structure`` with {{VRA_DASHBOARD_URL}} replaced.

    Resolves the placeholder to the live dashboard URL for ``assessment_id`` so
    the report's Dashboard Reference links straight into the dashboard. If no
    dashboard base URL is configured, the placeholder is left as-is.
    """
    from vra.dashboard import dashboard_url

    url = dashboard_url(assessment_id)
    if not url:
        return structure

    def _walk(node):
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if isinstance(node, str):
            return node.replace(DASHBOARD_LINK_PLACEHOLDER, url)
        return node

    return _walk(structure)
