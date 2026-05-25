import logging

from policy_hub.templates import get_template

logger = logging.getLogger(__name__)


def _replicate_sections(existing_sections: list, doc_type: str) -> list:
    """
    Sync existing sections list to the current template for doc_type.
    - Template sections: added (empty) if missing, reordered to match template order
    - Extra sections not in template: appended at end (preserved, not deleted)
    - Existing section content: kept exactly as-is
    """
    template_defs = get_template(doc_type)
    existing_by_id = {s["id"]: s for s in (existing_sections or [])}
    template_ids = {sd.id for sd in template_defs}

    result = []
    for sd in template_defs:
        if sd.id in existing_by_id:
            result.append(existing_by_id[sd.id])
        else:
            new_sec = {"id": sd.id, "title": sd.title, "kind": sd.kind, "body_html": ""}
            if sd.kind in ("statements", "steps"):
                new_sec["statements"] = []
            result.append(new_sec)

    # Append user-added sections not in the template (don't delete them)
    for sec in (existing_sections or []):
        if sec["id"] not in template_ids:
            result.append(sec)

    return result
