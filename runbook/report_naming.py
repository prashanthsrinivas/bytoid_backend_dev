"""Individual report-name generation for runbook reports.

Kept dependency-light (stdlib + a lazy LLM import) so it can be unit-tested in
isolation without dragging in the rest of the runbook infra. Used by the two
runbook generation paths (helper.py / helper2.py) to give each report a
distinct, human-readable name instead of all reports sharing the runbook name.
"""

import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _extract_first_paragraph(merged_result):
    """Return the first substantive paragraph of plain text from a report blob.

    Report content lives in merged_result['blocks'][*]['micro_blocks'][*]['html'].
    Strips tags and skips tiny heading-only blocks so the LLM gets real prose.
    """
    blocks = (merged_result or {}).get("blocks") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        html_parts = [
            mb.get("html") or ""
            for mb in (block.get("micro_blocks") or [])
            if isinstance(mb, dict)
        ]
        text = re.sub(r"<[^>]+>", " ", " ".join(html_parts))
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) >= 20:  # skip empty / heading-only blocks
            return text
    return ""


def _clean_descriptor(text):
    """Normalize the LLM descriptor to at most 3-4 clean words."""
    if not text or text.strip().upper() == "INSUFFICIENT":
        return ""
    first_line = text.strip().splitlines()[0]
    first_line = first_line.strip().strip('"').strip("'").strip(".")
    words = first_line.split()
    return " ".join(words[:4]).strip()


async def build_report_name(runbook_name, merged_result, credits, user_id):
    """Build an individual report name: '<runbook> — <2-3 word AI descriptor>'.

    The descriptor is derived by an LLM from the report's first paragraph so two
    reports of the same runbook get distinct names. Best-effort and never raises:
    on empty content / insufficient credits / any error it falls back to a
    timestamped name so names are always unique. The result is editable via
    the /result/<id>/rename endpoint.
    """
    base = (runbook_name or "Report").strip() or "Report"
    fallback = f"{base} — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"
    try:
        first_text = _extract_first_paragraph(merged_result)
        if not first_text:
            return fallback

        from utils.fireworkzz import get_fireworks_response

        prompt = (
            "From the first paragraph of an assessment report below, extract a "
            "2-3 word descriptor naming the specific subject being assessed "
            "(e.g. the system, vendor, product, or process). Return ONLY those "
            "2-3 words — no punctuation, quotes, or other text.\n\n"
            f"Paragraph:\n{first_text[:1500]}"
        )
        descriptor = await get_fireworks_response(prompt, "user", credits, user_id)
        descriptor = _clean_descriptor(descriptor)
        return f"{base} — {descriptor}" if descriptor else fallback
    except Exception:
        logger.warning("build_report_name failed; using fallback", exc_info=True)
        return fallback
